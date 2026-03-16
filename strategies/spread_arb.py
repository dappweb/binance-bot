"""Spread (Cross-Pair) Arbitrage Strategy.

Detects price discrepancies between related trading pairs.
For example, if ETH/USDT and ETH/BUSD have different prices,
we can buy on the cheaper pair and sell on the expensive one.
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

from config.settings import get_settings
from core.exchange import ExchangeClient
from core.price_stream import PriceBook
from utils.logger import get_logger

logger = get_logger("spread_arb")


@dataclass
class SpreadOpportunity:
    """A detected spread arbitrage opportunity."""
    buy_symbol: str
    sell_symbol: str
    buy_price: float  # ask on cheap side
    sell_price: float  # bid on expensive side
    spread_pct: float
    net_profit_pct: float
    quantity: float
    estimated_profit_usdt: float
    timestamp: float = field(default_factory=time.time)

    def __repr__(self) -> str:
        return (
            f"SpreadArb(buy={self.buy_symbol}@{self.buy_price:.6f}, "
            f"sell={self.sell_symbol}@{self.sell_price:.6f}, "
            f"spread={self.spread_pct:.4f}%, "
            f"profit=${self.estimated_profit_usdt:.4f})"
        )


# Default trading fee; can be overridden with BNB discount
TRADING_FEE_DEFAULT = 0.001
BNB_FEE = 0.00075


class SpreadScanner:
    """Scans for cross-pair spread arbitrage opportunities."""

    def __init__(self, exchange: ExchangeClient):
        self.exchange = exchange
        self.settings = get_settings()
        self._stablecoin_groups: dict[str, list[str]] = {}
        self._fee_rate = TRADING_FEE_DEFAULT

    def set_bnb_fee(self, use_bnb: bool = True):
        """Enable or disable BNB fee discount."""
        self._fee_rate = BNB_FEE if use_bnb else TRADING_FEE_DEFAULT

    def discover_pairs(self):
        """Discover related trading pairs that could have spread opportunities.

        Groups symbols by base asset across different quote currencies
        (e.g., ETHUSDT, ETHBUSD, ETHUSDC).
        """
        symbols = self.exchange.get_all_symbols()
        stablecoins = {"USDT", "BUSD", "USDC", "TUSD", "FDUSD"}

        # Group by base asset
        base_groups: dict[str, list[str]] = {}
        for symbol, info in symbols.items():
            if info.quote_asset in stablecoins:
                if info.base_asset not in base_groups:
                    base_groups[info.base_asset] = []
                base_groups[info.base_asset].append(symbol)

        # Keep only groups with 2+ stablecoin pairs
        self._stablecoin_groups = {
            base: pairs
            for base, pairs in base_groups.items()
            if len(pairs) >= 2
        }

        logger.info(
            f"Found {len(self._stablecoin_groups)} assets with multiple "
            f"stablecoin pairs for spread arbitrage"
        )
        return self._stablecoin_groups

    def scan_opportunities(
        self,
        price_book: PriceBook,
        min_profit: Optional[float] = None,
    ) -> list[SpreadOpportunity]:
        """Scan for spread arbitrage opportunities."""
        if min_profit is None:
            min_profit = self.settings.min_profit_threshold

        opportunities = []
        trade_amount = self.settings.trade_amount_usdt

        for base_asset, symbols in self._stablecoin_groups.items():
            prices = []
            for symbol in symbols:
                data = price_book.get(symbol)
                if data and data["bid"] > 0 and data["ask"] > 0:
                    prices.append((symbol, data))

            if len(prices) < 2:
                continue

            # Compare all pairs
            for i in range(len(prices)):
                for j in range(len(prices)):
                    if i == j:
                        continue

                    buy_sym, buy_data = prices[i]
                    sell_sym, sell_data = prices[j]

                    buy_price = buy_data["ask"]  # What we pay
                    sell_price = sell_data["bid"]  # What we receive

                    if buy_price <= 0:
                        continue

                    spread = (sell_price - buy_price) / buy_price
                    net_profit = spread - (2 * self._fee_rate)

                    if net_profit >= min_profit:
                        quantity = trade_amount / buy_price
                        est_profit = trade_amount * net_profit

                        opportunities.append(SpreadOpportunity(
                            buy_symbol=buy_sym,
                            sell_symbol=sell_sym,
                            buy_price=buy_price,
                            sell_price=sell_price,
                            spread_pct=spread * 100,
                            net_profit_pct=net_profit * 100,
                            quantity=quantity,
                            estimated_profit_usdt=est_profit,
                        ))

        opportunities.sort(key=lambda x: x.net_profit_pct, reverse=True)
        return opportunities

    def get_watched_symbols(self) -> list[str]:
        """Get all symbols needed for spread scanning."""
        symbols = set()
        for pairs in self._stablecoin_groups.values():
            symbols.update(pairs)
        return list(symbols)
