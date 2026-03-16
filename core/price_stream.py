"""Price Stream Manager - Real-time price feeds via WebSocket and REST."""

import asyncio
import time
from typing import Callable, Optional

try:
    import orjson
    def json_loads(data):
        return orjson.loads(data)
except ImportError:
    import json
    def json_loads(data):
        return json.loads(data)

from utils.logger import get_logger
from core.exchange import ExchangeClient

logger = get_logger("price_stream")


class PriceBook:
    """Thread-safe in-memory orderbook ticker cache.
    
    In single-threaded asyncio, dict writes are atomic in CPython.
    We use a lightweight approach: direct dict assignment for speed,
    and track update timestamps for staleness detection.
    """

    def __init__(self):
        self._prices: dict[str, dict] = {}
        self._last_update: dict[str, float] = {}

    async def update(self, symbol: str, bid: float, ask: float,
                     bid_qty: float = 0.0, ask_qty: float = 0.0):
        """Update price for a symbol. Lock-free for asyncio single-thread."""
        self._prices[symbol] = {
            "bid": bid,
            "ask": ask,
            "bid_qty": bid_qty,
            "ask_qty": ask_qty,
            "mid": (bid + ask) / 2 if bid > 0 and ask > 0 else 0,
            "spread": (ask - bid) / bid if bid > 0 else 0,
        }
        self._last_update[symbol] = time.monotonic()

    def get(self, symbol: str) -> Optional[dict]:
        """Get current price data for a symbol."""
        return self._prices.get(symbol)

    def get_bid(self, symbol: str) -> float:
        """Get best bid price."""
        data = self._prices.get(symbol)
        return data["bid"] if data else 0.0

    def get_ask(self, symbol: str) -> float:
        """Get best ask price."""
        data = self._prices.get(symbol)
        return data["ask"] if data else 0.0

    def get_mid(self, symbol: str) -> float:
        """Get mid price."""
        data = self._prices.get(symbol)
        return data["mid"] if data else 0.0

    def get_all(self) -> dict[str, dict]:
        """Get all prices."""
        return self._prices.copy()

    def is_stale(self, symbol: str, max_age: float = 5.0) -> bool:
        """Check if price data is stale."""
        last = self._last_update.get(symbol, 0)
        return (time.monotonic() - last) > max_age

    @property
    def symbols(self) -> list[str]:
        return list(self._prices.keys())

    @property
    def count(self) -> int:
        return len(self._prices)

    def are_prices_fresh(self, symbols: list[str], max_age: float = 3.0) -> bool:
        """Check if ALL given symbols have fresh price data.
        
        Used before executing trades to ensure prices haven't gone stale.
        """
        now = time.monotonic()
        for sym in symbols:
            last = self._last_update.get(sym, 0)
            if (now - last) > max_age:
                return False
        return True

    def get_age(self, symbol: str) -> float:
        """Get age of price data in seconds."""
        last = self._last_update.get(symbol, 0)
        return time.monotonic() - last if last > 0 else float('inf')


class PriceStreamManager:
    """Manages real-time price feeds using REST polling and WebSocket."""

    def __init__(self, exchange: ExchangeClient):
        self.exchange = exchange
        self.price_book = PriceBook()
        self._running = False
        self._ws_task: Optional[asyncio.Task] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._callbacks: list[Callable] = []
        self._watched_symbols: set[str] = set()
        self._health_callback: Optional[Callable] = None
        self._ws_connected = False  # Track WebSocket connection status

    def set_health_callback(self, callback: Callable):
        """Set a callback to report health (e.g., heartbeat)."""
        self._health_callback = callback

    def watch_symbols(self, symbols: list[str]):
        """Add symbols to watch list."""
        self._watched_symbols.update(symbols)
        logger.info(f"Watching {len(self._watched_symbols)} symbols")

    def on_price_update(self, callback: Callable):
        """Register a callback for price updates."""
        self._callbacks.append(callback)

    async def start(self):
        """Start the price stream."""
        self._running = True

        # Initial price load
        await self._poll_prices()

        # Start WebSocket stream
        self._ws_task = asyncio.create_task(self._ws_stream())

        # Start REST fallback polling
        self._poll_task = asyncio.create_task(self._poll_loop())

        logger.info("Price stream started")

    async def stop(self):
        """Stop the price stream."""
        self._running = False
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        logger.info("Price stream stopped")

    async def _poll_prices(self):
        """Poll all prices via REST API."""
        try:
            tickers = await self.exchange.get_all_orderbook_tickers()
            for symbol, data in tickers.items():
                if not self._watched_symbols or symbol in self._watched_symbols:
                    await self.price_book.update(
                        symbol,
                        data["bid"],
                        data["ask"],
                        data["bid_qty"],
                        data["ask_qty"],
                    )
            # Report health
            if self._health_callback:
                self._health_callback()
            logger.debug(f"Polled {len(tickers)} prices")
        except Exception as e:
            logger.error(f"Price poll error: {e}")

    async def _poll_loop(self):
        """Continuous polling loop as fallback.
        
        When WebSocket is connected, reduces polling frequency to save
        API rate limit for order execution. Falls back to fast polling
        when WebSocket disconnects.
        """
        while self._running:
            try:
                await self._poll_prices()
                # Notify callbacks
                for cb in self._callbacks:
                    try:
                        if asyncio.iscoroutinefunction(cb):
                            await cb(self.price_book)
                        else:
                            cb(self.price_book)
                    except Exception as e:
                        logger.error(f"Price callback error: {e}")
                # Adaptive polling: slow when WS is active, fast when it's not
                poll_interval = 10.0 if self._ws_connected else 1.0
                await asyncio.sleep(poll_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Poll loop error: {e}")
                await asyncio.sleep(5.0)

    async def _ws_stream(self):
        """WebSocket stream for real-time price updates."""
        try:
            import websockets
        except ImportError:
            logger.warning("websockets not installed, using REST polling only")
            return

        # Use combined streams for specific symbols instead of all bookTicker
        if self._watched_symbols:
            streams = "/".join(
                [f"{s.lower()}@bookTicker" for s in self._watched_symbols]
            )
            if self.exchange.settings.binance_testnet:
                ws_url = f"wss://testnet.binance.vision/stream?streams={streams}"
            else:
                ws_url = f"wss://stream.binance.com:9443/stream?streams={streams}"
            use_combined = True
        else:
            if self.exchange.settings.binance_testnet:
                ws_url = "wss://testnet.binance.vision/ws/!bookTicker"
            else:
                ws_url = "wss://stream.binance.com:9443/ws/!bookTicker"
            use_combined = False

        while self._running:
            try:
                async with websockets.connect(
                    ws_url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                    max_size=2**20,  # 1MB max message size
                    compression=None,  # Disable compression for lower latency
                ) as ws:
                    logger.info(f"WebSocket connected: {ws_url}")
                    self._ws_connected = True
                    async for message in ws:
                        if not self._running:
                            break
                        try:
                            raw = json_loads(message)
                            # Combined stream wraps data in {"stream": ..., "data": ...}
                            data = raw.get("data", raw) if use_combined else raw
                            symbol = data.get("s", "")
                            if not self._watched_symbols or symbol in self._watched_symbols:
                                bid = float(data.get("b", 0))
                                ask = float(data.get("a", 0))
                                bid_qty = float(data.get("B", 0))
                                ask_qty = float(data.get("A", 0))
                                if bid > 0 and ask > 0:
                                    await self.price_book.update(
                                        symbol, bid, ask, bid_qty, ask_qty
                                    )
                        except (KeyError, ValueError, TypeError) as e:
                            logger.debug(f"WS message parse error: {e}")
            except asyncio.CancelledError:
                self._ws_connected = False
                break
            except Exception as e:
                self._ws_connected = False
                logger.warning(f"WebSocket error: {e}, reconnecting in 2s...")
                await asyncio.sleep(2.0)

        logger.info("WebSocket stream stopped")
