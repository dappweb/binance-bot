"""Risk Management Module - Protects capital and enforces trading limits."""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

from config.settings import get_settings
from core.exchange import ExchangeClient
from utils.logger import get_logger

logger = get_logger("risk_mgmt")


@dataclass
class DailyStats:
    """Daily trading statistics for risk management."""
    date: str = ""
    total_pnl: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    max_single_loss: float = 0.0
    max_single_win: float = 0.0
    peak_balance: float = 0.0
    current_drawdown: float = 0.0


class RiskManager:
    """Manages trading risk and enforces limits."""

    def __init__(self, exchange: ExchangeClient):
        self.exchange = exchange
        self.settings = get_settings()
        self._initial_balance: float = 0.0
        self._peak_balance: float = 0.0
        self._current_balance: float = 0.0
        self._daily_pnl: float = 0.0
        self._daily_trades: int = 0
        self._daily_losses: float = 0.0
        self._is_halted: bool = False
        self._halt_reason: str = ""
        self._last_balance_update: float = 0
        self._daily_stats = DailyStats()
        self._lock = asyncio.Lock()

    async def initialize(self):
        """Initialize risk manager with current account state."""
        await self._update_balance()
        self._initial_balance = self._current_balance
        self._peak_balance = self._current_balance
        self._daily_stats.peak_balance = self._current_balance

        logger.info(
            f"Risk manager initialized. "
            f"Balance: ${self._current_balance:.2f} USDT, "
            f"Max drawdown: {self.settings.max_drawdown_pct}%, "
            f"Daily loss limit: ${self.settings.daily_loss_limit_usdt}"
        )

    async def _update_balance(self):
        """Update current balance from exchange."""
        await self.exchange.update_balances()
        self._current_balance = self.exchange.get_balance(
            self.settings.quote_currency
        )
        self._last_balance_update = time.monotonic()

        if self._current_balance > self._peak_balance:
            self._peak_balance = self._current_balance

    async def can_trade(self, trade_amount: float = 0) -> tuple[bool, str]:
        """Check if trading is allowed based on risk rules.

        Returns:
            (can_trade, reason) tuple
        """
        async with self._lock:
            if self._is_halted:
                return False, f"Trading halted: {self._halt_reason}"

            # Refresh balance if stale (> 30 seconds)
            if time.monotonic() - self._last_balance_update > 30:
                await self._update_balance()

            # Check daily loss limit
            if abs(self._daily_losses) >= self.settings.daily_loss_limit_usdt:
                self._halt("Daily loss limit reached")
                return False, f"Daily loss limit ${self.settings.daily_loss_limit_usdt} reached"

            # Check max drawdown
            if self._peak_balance > 0:
                drawdown = (
                    (self._peak_balance - self._current_balance)
                    / self._peak_balance
                    * 100
                )
                if drawdown >= self.settings.max_drawdown_pct:
                    self._halt(f"Max drawdown {drawdown:.2f}% exceeded")
                    return False, (
                        f"Max drawdown {self.settings.max_drawdown_pct}% reached "
                        f"(current: {drawdown:.2f}%)"
                    )

            # Check sufficient balance
            if trade_amount > 0:
                if self._current_balance < trade_amount:
                    return False, (
                        f"Insufficient balance: ${self._current_balance:.2f} < "
                        f"${trade_amount:.2f}"
                    )

                # Check position size limit
                max_position = self._current_balance * (
                    self.settings.position_size_pct / 100
                )
                if trade_amount > max_position:
                    return False, (
                        f"Trade ${trade_amount:.2f} exceeds position limit "
                        f"${max_position:.2f} ({self.settings.position_size_pct}%)"
                    )

            return True, "OK"

    def record_trade(self, pnl: float):
        """Record a completed trade's P&L."""
        self._daily_pnl += pnl
        self._daily_trades += 1

        if pnl < 0:
            self._daily_losses += abs(pnl)
            self._daily_stats.losing_trades += 1
            if pnl < self._daily_stats.max_single_loss:
                self._daily_stats.max_single_loss = pnl
        else:
            self._daily_stats.winning_trades += 1
            if pnl > self._daily_stats.max_single_win:
                self._daily_stats.max_single_win = pnl

        self._daily_stats.total_pnl = self._daily_pnl
        self._daily_stats.total_trades = self._daily_trades

        logger.info(
            f"Trade P&L: ${pnl:+.4f} | "
            f"Daily P&L: ${self._daily_pnl:+.4f} | "
            f"Trades: {self._daily_trades}"
        )

    def get_max_trade_amount(self) -> float:
        """Get the maximum allowed trade amount."""
        max_by_position = self._current_balance * (
            self.settings.position_size_pct / 100
        )
        max_by_remaining_loss = self.settings.daily_loss_limit_usdt - abs(
            self._daily_losses
        )
        max_by_config = self.settings.trade_amount_usdt

        return max(0, min(max_by_position, max_by_remaining_loss, max_by_config))

    def _halt(self, reason: str):
        """Halt trading."""
        self._is_halted = True
        self._halt_reason = reason
        logger.warning(f"TRADING HALTED: {reason}")

    def resume(self):
        """Resume trading after halt."""
        self._is_halted = False
        self._halt_reason = ""
        logger.info("Trading resumed")

    def reset_daily(self):
        """Reset daily counters (call at start of new trading day)."""
        self._daily_pnl = 0.0
        self._daily_trades = 0
        self._daily_losses = 0.0
        self._daily_stats = DailyStats()
        if self._is_halted and "daily" in self._halt_reason.lower():
            self.resume()
        logger.info("Daily risk counters reset")

    @property
    def status(self) -> dict:
        """Get current risk status."""
        drawdown = 0
        if self._peak_balance > 0:
            drawdown = (
                (self._peak_balance - self._current_balance)
                / self._peak_balance
                * 100
            )

        return {
            "is_halted": self._is_halted,
            "halt_reason": self._halt_reason,
            "current_balance": self._current_balance,
            "initial_balance": self._initial_balance,
            "peak_balance": self._peak_balance,
            "current_drawdown_pct": drawdown,
            "daily_pnl": self._daily_pnl,
            "daily_trades": self._daily_trades,
            "daily_losses": self._daily_losses,
            "remaining_loss_budget": max(
                0, self.settings.daily_loss_limit_usdt - self._daily_losses
            ),
            "max_trade_amount": self.get_max_trade_amount(),
        }
