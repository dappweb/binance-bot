"""Helper utilities for the trading bot."""

import time
import asyncio
from decimal import Decimal, ROUND_DOWN
from functools import wraps
from typing import Any, Callable


def round_step_size(quantity: float, step_size: float) -> float:
    """Round quantity down to the nearest step size."""
    if step_size == 0:
        return quantity
    precision = len(str(step_size).rstrip("0").split(".")[-1])
    return float(Decimal(str(quantity)).quantize(Decimal(str(step_size)), rounding=ROUND_DOWN))


def round_price(price: float, tick_size: float) -> float:
    """Round price to the nearest tick size."""
    if tick_size == 0:
        return price
    precision = len(str(tick_size).rstrip("0").split(".")[-1])
    return float(Decimal(str(price)).quantize(Decimal(str(tick_size)), rounding=ROUND_DOWN))


def calculate_profit_pct(buy_price: float, sell_price: float, fees: float = 0.001) -> float:
    """Calculate profit percentage after fees.
    
    Args:
        buy_price: Entry price
        sell_price: Exit price 
        fees: Trading fee rate (default 0.1% = 0.001)
    
    Returns:
        Profit as a decimal ratio (e.g., 0.01 = 1%)
    """
    gross_profit = (sell_price - buy_price) / buy_price
    net_profit = gross_profit - (2 * fees)  # fee on buy and sell
    return net_profit


def retry_async(max_retries: int = 3, delay: float = 1.0, backoff: float = 2.0):
    """Async retry decorator with exponential backoff."""
    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = None
            current_delay = delay
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        await asyncio.sleep(current_delay)
                        current_delay *= backoff
            raise last_exception
        return wrapper
    return decorator


class RateLimiter:
    """Simple async rate limiter using token bucket algorithm."""

    def __init__(self, max_calls: int, period: float):
        self.max_calls = max_calls
        self.period = period
        self.calls: list[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self):
        """Acquire permission to make a call."""
        async with self._lock:
            now = time.monotonic()
            # Remove expired entries
            self.calls = [t for t in self.calls if now - t < self.period]
            if len(self.calls) >= self.max_calls:
                sleep_time = self.period - (now - self.calls[0])
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)
                self.calls = self.calls[1:]
            self.calls.append(time.monotonic())

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *args):
        pass


class Timer:
    """Context manager for timing operations."""

    def __init__(self, name: str = ""):
        self.name = name
        self.elapsed: float = 0

    def __enter__(self):
        self._start = time.perf_counter()
        return self

    def __exit__(self, *args):
        self.elapsed = time.perf_counter() - self._start

    @property
    def elapsed_ms(self) -> float:
        return self.elapsed * 1000
