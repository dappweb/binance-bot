"""Tests for risk manager."""

import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.risk_manager import RiskManager


def create_mock_exchange(balance: float = 1000.0):
    exchange = MagicMock()
    exchange.get_balance.return_value = balance
    exchange.update_balances = AsyncMock()
    return exchange


class TestRiskManager:
    @pytest.fixture
    def risk_mgr(self):
        exchange = create_mock_exchange(1000.0)
        rm = RiskManager(exchange)
        rm.settings = MagicMock()
        rm.settings.quote_currency = "USDT"
        rm.settings.max_drawdown_pct = 5.0
        rm.settings.daily_loss_limit_usdt = 50.0
        rm.settings.position_size_pct = 10.0
        rm.settings.trade_amount_usdt = 100.0
        rm._current_balance = 1000.0
        rm._initial_balance = 1000.0
        rm._peak_balance = 1000.0
        rm._last_balance_update = float('inf')  # Prevent stale refresh
        return rm

    @pytest.mark.asyncio
    async def test_can_trade_normal(self, risk_mgr):
        can, reason = await risk_mgr.can_trade(50.0)
        assert can is True
        assert reason == "OK"

    @pytest.mark.asyncio
    async def test_insufficient_balance(self, risk_mgr):
        can, reason = await risk_mgr.can_trade(2000.0)
        assert can is False
        assert "Insufficient" in reason

    @pytest.mark.asyncio
    async def test_position_size_limit(self, risk_mgr):
        # 10% of 1000 = 100, asking for 150
        can, reason = await risk_mgr.can_trade(150.0)
        assert can is False
        assert "position limit" in reason

    @pytest.mark.asyncio
    async def test_daily_loss_halt(self, risk_mgr):
        # Record enough losses to trigger halt
        risk_mgr.record_trade(-25.0)
        risk_mgr.record_trade(-30.0)

        can, reason = await risk_mgr.can_trade(10.0)
        assert can is False
        assert "Daily loss limit" in reason

    @pytest.mark.asyncio  
    async def test_drawdown_halt(self, risk_mgr):
        risk_mgr._peak_balance = 1000.0
        risk_mgr._current_balance = 940.0  # 6% drawdown > 5% limit

        can, reason = await risk_mgr.can_trade(10.0)
        assert can is False
        assert "drawdown" in reason.lower()

    def test_record_trade_pnl(self, risk_mgr):
        risk_mgr.record_trade(5.0)
        risk_mgr.record_trade(-2.0)

        status = risk_mgr.status
        assert status["daily_pnl"] == 3.0
        assert status["daily_trades"] == 2

    def test_reset_daily(self, risk_mgr):
        risk_mgr.record_trade(-10.0)
        risk_mgr.reset_daily()

        status = risk_mgr.status
        assert status["daily_pnl"] == 0.0
        assert status["daily_trades"] == 0

    def test_max_trade_amount(self, risk_mgr):
        max_amount = risk_mgr.get_max_trade_amount()
        # Should be min of position_size (100), daily_remaining (50), config (100)
        assert max_amount == 50.0  # daily loss limit is the smallest


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
