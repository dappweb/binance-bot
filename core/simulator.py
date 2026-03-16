"""Offline Simulation Module - Simulates market data and exchange behavior.

Provides realistic price simulation with injected arbitrage opportunities
to demonstrate the bot's scanning and execution capabilities without
requiring real API keys.
"""

import asyncio
import random
import time
import math
from typing import Optional

from utils.logger import get_logger

logger = get_logger("simulator")


# Realistic base prices for major crypto pairs
BASE_PRICES = {
    "BTCUSDT": 87500.0,
    "ETHUSDT": 3420.0,
    "BNBUSDT": 635.0,
    "SOLUSDT": 142.0,
    "XRPUSDT": 2.45,
    "ADAUSDT": 0.72,
    "DOGEUSDT": 0.165,
    "DOTUSDT": 7.85,
    "AVAXUSDT": 38.50,
    "LINKUSDT": 18.20,
    "MATICUSDT": 0.58,
    "LTCUSDT": 95.0,
    # Cross pairs
    "ETHBTC": 0.0391,
    "BNBBTC": 0.00726,
    "SOLBTC": 0.001623,
    "XRPBTC": 0.000028,
    "ADABTC": 0.00000823,
    "DOGEBTC": 0.00000189,
    "DOTBTC": 0.0000897,
    "AVAXBTC": 0.00044,
    "LINKBTC": 0.000208,
    "LTCBTC": 0.001086,
    "BNBETH": 0.1857,
    "SOLETH": 0.0415,
    "XRPETH": 0.000717,
    "ADAETH": 0.000211,
    "DOTETH": 0.002295,
    "LINKETH": 0.005322,
    "LTCETH": 0.02778,
}

# Symbol info specs: (base_asset, quote_asset, step_size, tick_size, min_qty, min_notional)
SYMBOL_SPECS = {
    "BTCUSDT": ("BTC", "USDT", 0.00001, 0.01, 0.00001, 10.0),
    "ETHUSDT": ("ETH", "USDT", 0.0001, 0.01, 0.0001, 10.0),
    "BNBUSDT": ("BNB", "USDT", 0.001, 0.01, 0.001, 10.0),
    "SOLUSDT": ("SOL", "USDT", 0.01, 0.01, 0.01, 10.0),
    "XRPUSDT": ("XRP", "USDT", 0.1, 0.0001, 0.1, 10.0),
    "ADAUSDT": ("ADA", "USDT", 0.1, 0.0001, 0.1, 10.0),
    "DOGEUSDT": ("DOGE", "USDT", 1.0, 0.00001, 1.0, 10.0),
    "DOTUSDT": ("DOT", "USDT", 0.01, 0.01, 0.01, 10.0),
    "AVAXUSDT": ("AVAX", "USDT", 0.01, 0.01, 0.01, 10.0),
    "LINKUSDT": ("LINK", "USDT", 0.01, 0.01, 0.01, 10.0),
    "MATICUSDT": ("MATIC", "USDT", 0.1, 0.0001, 0.1, 10.0),
    "LTCUSDT": ("LTC", "USDT", 0.001, 0.01, 0.001, 10.0),
    "ETHBTC": ("ETH", "BTC", 0.0001, 0.000001, 0.0001, 0.0001),
    "BNBBTC": ("BNB", "BTC", 0.001, 0.0000001, 0.001, 0.0001),
    "SOLBTC": ("SOL", "BTC", 0.01, 0.00000001, 0.01, 0.0001),
    "XRPBTC": ("XRP", "BTC", 0.1, 0.00000001, 0.1, 0.0001),
    "ADABTC": ("ADA", "BTC", 0.1, 0.00000001, 0.1, 0.0001),
    "DOGEBTC": ("DOGE", "BTC", 1.0, 0.00000001, 1.0, 0.0001),
    "DOTBTC": ("DOT", "BTC", 0.01, 0.0000001, 0.01, 0.0001),
    "AVAXBTC": ("AVAX", "BTC", 0.01, 0.0000001, 0.01, 0.0001),
    "LINKBTC": ("LINK", "BTC", 0.01, 0.0000001, 0.01, 0.0001),
    "LTCBTC": ("LTC", "BTC", 0.001, 0.0000001, 0.001, 0.0001),
    "BNBETH": ("BNB", "ETH", 0.001, 0.000001, 0.001, 0.001),
    "SOLETH": ("SOL", "ETH", 0.01, 0.000001, 0.01, 0.001),
    "XRPETH": ("XRP", "ETH", 0.1, 0.00000001, 0.1, 0.001),
    "ADAETH": ("ADA", "ETH", 0.1, 0.00000001, 0.1, 0.001),
    "DOTETH": ("DOT", "ETH", 0.01, 0.0000001, 0.01, 0.001),
    "LINKETH": ("LINK", "ETH", 0.01, 0.0000001, 0.01, 0.001),
    "LTCETH": ("LTC", "ETH", 0.001, 0.000001, 0.001, 0.001),
}


class SimSymbolInfo:
    """Simulated symbol trading rules."""

    def __init__(self, symbol: str, spec: tuple):
        self.symbol = symbol
        self.base_asset = spec[0]
        self.quote_asset = spec[1]
        self.step_size = spec[2]
        self.tick_size = spec[3]
        self.min_qty = spec[4]
        self.min_notional = spec[5]
        self.max_qty = 9999999.0
        self.min_price = 0.0
        self.max_price = 9999999.0
        self.status = "TRADING"

    @property
    def is_trading(self) -> bool:
        return True


class SimulatedExchange:
    """Simulated exchange client that generates realistic market data."""

    def __init__(self, initial_balance: float = 10000.0):
        self.initial_balance = initial_balance
        self._symbols: dict[str, SimSymbolInfo] = {}
        self._balances: dict[str, float] = {"USDT": initial_balance, "BNB": 1.0}
        self._current_prices: dict[str, float] = {}
        self._tick: int = 0
        self._total_fees: float = 0.0
        self._arb_injection_countdown: int = 0
        self._arb_injection_symbol: str = ""
        self._arb_injection_direction: int = 0

        # Settings proxy
        self.settings = type("S", (), {
            "binance_testnet": True,
            "binance_api_key": "sim_key",
            "binance_api_secret": "sim_secret",
        })()

    async def initialize(self):
        """Initialize simulated exchange."""
        logger.info("Initializing simulated exchange...")
        for symbol, spec in SYMBOL_SPECS.items():
            self._symbols[symbol] = SimSymbolInfo(symbol, spec)
        self._current_prices = BASE_PRICES.copy()
        logger.info(f"Loaded {len(self._symbols)} simulated trading pairs")
        logger.info(f"Initial balance: ${self.initial_balance:.2f} USDT + 1.0 BNB")

    def get_symbol_info(self, symbol: str) -> Optional[SimSymbolInfo]:
        return self._symbols.get(symbol)

    def get_all_symbols(self) -> dict[str, SimSymbolInfo]:
        return self._symbols.copy()

    def find_symbols_with_assets(self, base: str, quote: str) -> Optional[str]:
        direct = f"{base}{quote}"
        if direct in self._symbols:
            return direct
        reverse = f"{quote}{base}"
        if reverse in self._symbols:
            return reverse
        return None

    def get_balance(self, asset: str) -> float:
        return self._balances.get(asset, 0.0)

    def get_all_balances(self) -> dict[str, float]:
        return {k: v for k, v in self._balances.items() if v > 0}

    async def update_balances(self):
        pass  # Balances are updated instantly in simulation

    async def get_all_orderbook_tickers(self) -> dict[str, dict]:
        """Generate simulated orderbook tickers with realistic spreads."""
        self._tick += 1
        self._update_prices()
        result = {}
        for symbol, mid_price in self._current_prices.items():
            spread = self._get_spread(symbol, mid_price)
            bid = mid_price - spread / 2
            ask = mid_price + spread / 2
            bid_qty = random.uniform(0.5, 50.0)
            ask_qty = random.uniform(0.5, 50.0)
            result[symbol] = {
                "bid": bid,
                "ask": ask,
                "bid_qty": bid_qty,
                "ask_qty": ask_qty,
            }
        return result

    def _get_spread(self, symbol: str, price: float) -> float:
        """Get realistic bid-ask spread for a symbol."""
        # Major pairs have tighter spreads
        if symbol in ("BTCUSDT", "ETHUSDT"):
            return price * 0.0001  # 0.01%
        elif "USDT" in symbol:
            return price * 0.0003  # 0.03%
        elif "BTC" in symbol:
            return price * 0.0005  # 0.05%
        else:
            return price * 0.0008  # 0.08%

    def _update_prices(self):
        """Update simulated prices with realistic random walk + periodic arbitrage injection."""
        t = self._tick * 0.1  # Time variable

        for symbol in list(self._current_prices.keys()):
            base_price = BASE_PRICES[symbol]

            # Brownian motion + mean reversion
            noise = random.gauss(0, 1) * 0.0003  # 0.03% per tick
            trend = math.sin(t * 0.1) * 0.001  # Slow oscillation
            mean_revert = (base_price - self._current_prices[symbol]) / base_price * 0.01

            self._current_prices[symbol] *= (1 + noise + trend + mean_revert)

        # Periodically inject arbitrage opportunities
        self._arb_injection_countdown -= 1
        if self._arb_injection_countdown <= 0:
            self._inject_arbitrage_opportunity()
            self._arb_injection_countdown = random.randint(5, 15)  # Every 5-15 ticks

    def _inject_arbitrage_opportunity(self):
        """Inject a small arbitrage opportunity into the price data.

        Creates a momentary mispricing in a triangle to simulate
        a real arbitrage window.
        """
        # Pick a triangle: USDT -> X -> Y -> USDT
        triangles = [
            ("BTC", "ETH"),
            ("BTC", "BNB"),
            ("BTC", "SOL"),
            ("BTC", "LINK"),
            ("BTC", "LTC"),
            ("ETH", "BNB"),
            ("ETH", "LINK"),
            ("ETH", "DOT"),
        ]

        a, b = random.choice(triangles)
        cross_symbol = f"{b}{a}"
        if cross_symbol not in self._current_prices:
            cross_symbol = f"{a}{b}"
            if cross_symbol not in self._current_prices:
                return

        # Create mispricing: adjust cross pair by 0.3-0.8%
        direction = random.choice([-1, 1])
        magnitude = random.uniform(0.003, 0.008)
        adjustment = 1 + direction * magnitude

        self._current_prices[cross_symbol] *= adjustment
        self._arb_injection_symbol = cross_symbol
        self._arb_injection_direction = direction

        logger.debug(
            f"Injected arb opportunity: {cross_symbol} "
            f"{'↑' if direction > 0 else '↓'}{magnitude*100:.3f}%"
        )

    async def place_market_order(self, symbol: str, side: str, quantity: float) -> dict:
        """Simulate a market order execution.
        
        Matches Binance behavior:
        - BUY: commission deducted from received base asset
        - SELL: commission deducted from received quote asset
        Commission is reported in the received asset's unit.
        """
        mid_price = self._current_prices.get(symbol, 0)
        if mid_price <= 0:
            raise ValueError(f"No price for {symbol}")

        spread = self._get_spread(symbol, mid_price)

        # Simulate slippage: 0.01-0.05%
        slippage = random.uniform(0.0001, 0.0005)

        if side == "BUY":
            fill_price = mid_price + spread / 2 + mid_price * slippage
        else:
            fill_price = mid_price - spread / 2 - mid_price * slippage

        sym_info = self._symbols[symbol]
        fee_rate = 0.00075  # BNB fee rate

        # Update balances (matching Binance convention)
        if side == "BUY":
            cost = quantity * fill_price
            commission = quantity * fee_rate  # Commission in base (received) asset
            commission_asset = sym_info.base_asset
            self._balances[sym_info.quote_asset] = self._balances.get(sym_info.quote_asset, 0) - cost
            self._balances[sym_info.base_asset] = self._balances.get(sym_info.base_asset, 0) + quantity - commission
            commission_value = commission * fill_price
        else:
            proceeds = quantity * fill_price
            commission = proceeds * fee_rate  # Commission in quote (received) asset
            commission_asset = sym_info.quote_asset
            self._balances[sym_info.base_asset] = self._balances.get(sym_info.base_asset, 0) - quantity
            self._balances[sym_info.quote_asset] = self._balances.get(sym_info.quote_asset, 0) + proceeds - commission
            commission_value = commission

        # Clean up tiny/negative balances
        for asset in list(self._balances.keys()):
            if abs(self._balances[asset]) < 1e-10:
                self._balances[asset] = 0.0

        self._total_fees += commission_value

        logger.info(
            f"[SIM] {side} {symbol}: qty={quantity:.8f} price={fill_price:.8f} "
            f"commission={commission:.8f} ({commission_asset})"
        )

        return {
            "orderId": f"sim_{int(time.time()*1000)}_{random.randint(1000,9999)}",
            "symbol": symbol,
            "side": side,
            "price": fill_price,
            "quantity": quantity,
            "commission": commission,
            "commissionAsset": commission_asset,
            "status": "FILLED",
        }

    async def close(self):
        logger.info("Simulated exchange closed")
