"""Binance Exchange Client - Handles all API interactions."""

import asyncio
from typing import Optional

from binance import AsyncClient
from binance.exceptions import BinanceAPIException, BinanceOrderException

from config.settings import get_settings
from utils.logger import get_logger
from utils.helpers import RateLimiter, retry_async, round_step_size, round_price

logger = get_logger("exchange")


class SymbolInfo:
    """Cached symbol trading rules."""

    def __init__(self, info: dict):
        self.symbol: str = info["symbol"]
        self.base_asset: str = info["baseAsset"]
        self.quote_asset: str = info["quoteAsset"]
        self.status: str = info["status"]

        # Extract filters
        self.min_qty: float = 0.0
        self.max_qty: float = 0.0
        self.step_size: float = 0.0
        self.min_notional: float = 0.0
        self.tick_size: float = 0.0
        self.min_price: float = 0.0
        self.max_price: float = 0.0

        for f in info.get("filters", []):
            filter_type = f["filterType"]
            if filter_type == "LOT_SIZE":
                self.min_qty = float(f["minQty"])
                self.max_qty = float(f["maxQty"])
                self.step_size = float(f["stepSize"])
            elif filter_type == "PRICE_FILTER":
                self.min_price = float(f["minPrice"])
                self.max_price = float(f["maxPrice"])
                self.tick_size = float(f["tickSize"])
            elif filter_type in ("MIN_NOTIONAL", "NOTIONAL"):
                self.min_notional = float(f.get("minNotional", 0))

    @property
    def is_trading(self) -> bool:
        return self.status == "TRADING"


class ExchangeClient:
    """Async wrapper around Binance API client."""

    def __init__(self):
        self.settings = get_settings()
        self._client: Optional[AsyncClient] = None
        self._symbols: dict[str, SymbolInfo] = {}
        self._rate_limiter = RateLimiter(max_calls=18, period=1.0)
        self._balances: dict[str, float] = {}

    async def initialize(self):
        """Initialize the Binance client and load exchange info."""
        logger.info("Initializing Binance async client...")

        self._client = await AsyncClient.create(
            api_key=self.settings.binance_api_key,
            api_secret=self.settings.binance_api_secret,
            testnet=self.settings.binance_testnet,
        )

        if self.settings.binance_testnet:
            self._client.API_URL = "https://testnet.binance.vision/api"
            logger.info("Using TESTNET environment")
        else:
            logger.warning("Using PRODUCTION environment!")

        await self._load_exchange_info()
        await self.update_balances()
        logger.info(
            f"Exchange client initialized. {len(self._symbols)} symbols loaded."
        )

    async def _load_exchange_info(self):
        """Load and cache exchange trading rules."""
        async with self._rate_limiter:
            info = await self._client.get_exchange_info()

        for s in info["symbols"]:
            symbol_info = SymbolInfo(s)
            if symbol_info.is_trading:
                self._symbols[symbol_info.symbol] = symbol_info

        logger.info(f"Loaded {len(self._symbols)} active trading pairs")

    def get_symbol_info(self, symbol: str) -> Optional[SymbolInfo]:
        """Get cached symbol info."""
        return self._symbols.get(symbol)

    def get_all_symbols(self) -> dict[str, SymbolInfo]:
        """Get all cached symbols."""
        return self._symbols

    def find_symbols_with_assets(self, base: str, quote: str) -> Optional[str]:
        """Find a trading pair symbol for given base and quote assets."""
        direct = f"{base}{quote}"
        if direct in self._symbols:
            return direct
        reverse = f"{quote}{base}"
        if reverse in self._symbols:
            return reverse
        return None

    @retry_async(max_retries=3, delay=0.5)
    async def get_orderbook_ticker(self, symbol: str) -> dict:
        """Get best bid/ask for a symbol."""
        async with self._rate_limiter:
            ticker = await self._client.get_orderbook_ticker(symbol=symbol)
        return {
            "bid": float(ticker["bidPrice"]),
            "ask": float(ticker["askPrice"]),
            "bid_qty": float(ticker["bidQty"]),
            "ask_qty": float(ticker["askQty"]),
        }

    @retry_async(max_retries=3, delay=0.5)
    async def get_all_tickers(self) -> dict[str, float]:
        """Get all current prices."""
        async with self._rate_limiter:
            tickers = await self._client.get_all_tickers()
        return {t["symbol"]: float(t["price"]) for t in tickers}

    @retry_async(max_retries=3, delay=0.5)
    async def get_all_orderbook_tickers(self) -> dict[str, dict]:
        """Get all orderbook tickers (bid/ask)."""
        async with self._rate_limiter:
            tickers = await self._client.get_orderbook_tickers()
        result = {}
        for t in tickers:
            result[t["symbol"]] = {
                "bid": float(t["bidPrice"]),
                "ask": float(t["askPrice"]),
                "bid_qty": float(t["bidQty"]),
                "ask_qty": float(t["askQty"]),
            }
        return result

    async def update_balances(self):
        """Update account balances."""
        try:
            async with self._rate_limiter:
                account = await self._client.get_account()
            self._balances = {}
            for b in account.get("balances", []):
                free = float(b["free"])
                if free > 0:
                    self._balances[b["asset"]] = free
            logger.debug(f"Balances updated: {len(self._balances)} assets")
        except Exception as e:
            logger.error(f"Failed to update balances: {e}")

    def get_balance(self, asset: str) -> float:
        """Get free balance for an asset."""
        return self._balances.get(asset, 0.0)

    def get_all_balances(self) -> dict[str, float]:
        """Get all non-zero balances."""
        return self._balances.copy()

    @retry_async(max_retries=2, delay=0.3)
    async def place_market_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
    ) -> dict:
        """Place a market order.

        Args:
            symbol: Trading pair (e.g., 'BTCUSDT')
            side: 'BUY' or 'SELL'
            quantity: Order quantity

        Returns:
            Order result dict
        """
        symbol_info = self.get_symbol_info(symbol)
        if not symbol_info:
            raise ValueError(f"Unknown symbol: {symbol}")

        # Round quantity to valid step size
        quantity = round_step_size(quantity, symbol_info.step_size)

        if quantity < symbol_info.min_qty:
            raise ValueError(
                f"Quantity {quantity} below minimum {symbol_info.min_qty} for {symbol}"
            )

        logger.info(f"Placing {side} market order: {symbol} qty={quantity}")

        try:
            async with self._rate_limiter:
                order = await self._client.create_order(
                    symbol=symbol,
                    side=side,
                    type="MARKET",
                    quantity=quantity,
                )

            filled_price = float(order.get("fills", [{}])[0].get("price", 0))
            filled_qty = float(order.get("executedQty", 0))
            commission = sum(float(f.get("commission", 0)) for f in order.get("fills", []))
            # Track commission asset for adaptive execution
            commission_asset = ""
            fills = order.get("fills", [])
            if fills:
                commission_asset = fills[0].get("commissionAsset", "")

            logger.info(
                f"Order filled: {side} {symbol} qty={filled_qty} "
                f"price={filled_price} commission={commission} ({commission_asset})"
            )

            return {
                "orderId": order["orderId"],
                "symbol": symbol,
                "side": side,
                "price": filled_price,
                "quantity": filled_qty,
                "commission": commission,
                "commissionAsset": commission_asset,
                "status": order["status"],
            }

        except BinanceOrderException as e:
            logger.error(f"Order error for {symbol}: {e}")
            raise
        except BinanceAPIException as e:
            logger.error(f"API error placing order: {e}")
            raise

    @retry_async(max_retries=2, delay=0.3)
    async def place_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
    ) -> dict:
        """Place a limit order."""
        symbol_info = self.get_symbol_info(symbol)
        if not symbol_info:
            raise ValueError(f"Unknown symbol: {symbol}")

        quantity = round_step_size(quantity, symbol_info.step_size)
        price = round_price(price, symbol_info.tick_size)

        if quantity < symbol_info.min_qty:
            raise ValueError(
                f"Quantity {quantity} below minimum {symbol_info.min_qty} for {symbol}"
            )

        logger.info(
            f"Placing {side} limit order: {symbol} qty={quantity} price={price}"
        )

        try:
            async with self._rate_limiter:
                order = await self._client.create_order(
                    symbol=symbol,
                    side=side,
                    type="LIMIT",
                    timeInForce="GTC",
                    quantity=quantity,
                    price=str(price),
                )

            return {
                "orderId": order["orderId"],
                "symbol": symbol,
                "side": side,
                "price": price,
                "quantity": quantity,
                "status": order["status"],
            }

        except (BinanceOrderException, BinanceAPIException) as e:
            logger.error(f"Order error for {symbol}: {e}")
            raise

    async def cancel_order(self, symbol: str, order_id: int) -> dict:
        """Cancel an open order."""
        try:
            async with self._rate_limiter:
                result = await self._client.cancel_order(
                    symbol=symbol,
                    orderId=order_id,
                )
            logger.info(f"Cancelled order {order_id} for {symbol}")
            return result
        except BinanceAPIException as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            raise

    async def get_order_status(self, symbol: str, order_id: int) -> dict:
        """Get status of an order."""
        async with self._rate_limiter:
            return await self._client.get_order(
                symbol=symbol,
                orderId=order_id,
            )

    async def close(self):
        """Close the client connection."""
        if self._client:
            await self._client.close_connection()
            logger.info("Exchange client closed")
