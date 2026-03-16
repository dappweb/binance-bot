"""Tests for utility helpers."""

import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.helpers import round_step_size, round_price, calculate_profit_pct, Timer


class TestRoundStepSize:
    def test_basic_rounding(self):
        assert round_step_size(1.23456, 0.01) == 1.23
        assert round_step_size(1.23456, 0.001) == 1.234
        assert round_step_size(1.23456, 0.1) == 1.2

    def test_rounds_down(self):
        assert round_step_size(1.999, 0.01) == 1.99
        assert round_step_size(0.12345, 0.0001) == 0.1234

    def test_zero_step(self):
        assert round_step_size(1.234, 0) == 1.234


class TestRoundPrice:
    def test_basic_rounding(self):
        assert round_price(50000.123, 0.01) == 50000.12
        assert round_price(3200.5678, 0.1) == 3200.5

    def test_zero_tick(self):
        assert round_price(100.5, 0) == 100.5


class TestCalculateProfit:
    def test_positive_profit(self):
        profit = calculate_profit_pct(100, 102, fees=0.001)
        # Gross: 2%, Net: 2% - 0.2% = 1.8%
        assert abs(profit - 0.018) < 0.0001

    def test_negative_profit(self):
        profit = calculate_profit_pct(100, 100.1, fees=0.001)
        # Gross: 0.1%, Net: 0.1% - 0.2% = -0.1%
        assert profit < 0

    def test_zero_fees(self):
        profit = calculate_profit_pct(100, 105, fees=0)
        assert abs(profit - 0.05) < 0.0001


class TestTimer:
    def test_timer_measures(self):
        import time
        with Timer("test") as t:
            time.sleep(0.01)
        assert t.elapsed > 0.005
        assert t.elapsed_ms > 5


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
