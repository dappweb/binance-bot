"""Triangular Arbitrage Strategy Engine.

Detects and executes triangular arbitrage opportunities across three trading pairs.

Example triangle: USDT -> BTC -> ETH -> USDT
  Step 1: Buy BTC with USDT (BTCUSDT)
  Step 2: Buy ETH with BTC (ETHBTC)
  Step 3: Sell ETH for USDT (ETHUSDT)

If the product of rates minus fees > 1.0, there is an arbitrage opportunity.
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional
from itertools import permutations

from config.settings import get_settings
from core.exchange import ExchangeClient, SymbolInfo
from core.price_stream import PriceBook
from utils.logger import get_logger
from utils.helpers import Timer

logger = get_logger("triangular_arb")

# Binance trading fee (BNB discount: 0.075%, standard: 0.1%)
TRADING_FEE = 0.001  # 0.1%
BNB_FEE = 0.00075  # 0.075% with BNB discount


@dataclass
class ArbLeg:
    """Single leg of a triangular arbitrage trade."""
    symbol: str
    side: str  # BUY or SELL
    price: float
    quantity: float
    base_asset: str
    quote_asset: str


@dataclass
class ArbOpportunity:
    """A detected triangular arbitrage opportunity."""
    triangle: tuple[str, str, str]  # (currency_A, currency_B, currency_C)
    legs: list[ArbLeg]
    profit_ratio: float  # Net profit ratio after fees
    profit_usdt: float  # Expected profit in USDT
    input_amount: float  # Input amount in quote currency
    timestamp: float = field(default_factory=time.time)

    @property
    def profit_pct(self) -> float:
        return self.profit_ratio * 100

    def __repr__(self) -> str:
        path = " -> ".join(self.triangle) + f" -> {self.triangle[0]}"
        return (
            f"ArbOpportunity({path}, "
            f"profit={self.profit_pct:.4f}%, "
            f"~${self.profit_usdt:.4f})"
        )


class TriangleScanner:
    """Scans for triangular arbitrage opportunities."""

    def __init__(self, exchange: ExchangeClient):
        self.exchange = exchange
        self.settings = get_settings()
        self._triangles: list[tuple[str, str, str]] = []
        self._symbol_map: dict[str, SymbolInfo] = {}
        self._pair_lookup: dict[tuple[str, str], str] = {}
        self._fee_rate = TRADING_FEE
        self._fee_multiplier = 1 - TRADING_FEE  # Cached fee multiplier
        # Pre-computed minimum product of bid/ask ratios needed for profit
        # For 3 legs with fees: need product > 1 / fee_mult^3
        self._min_product = 1.0 / ((1 - TRADING_FEE) ** 3)

    def set_bnb_fee(self, use_bnb: bool = True):
        """Enable or disable BNB fee discount."""
        self._fee_rate = BNB_FEE if use_bnb else TRADING_FEE
        self._fee_multiplier = 1 - self._fee_rate
        self._min_product = 1.0 / (self._fee_multiplier ** 3)
        logger.info(f"Trading fee set to {self._fee_rate * 100}%")

    def discover_triangles(self) -> list[tuple[str, str, str]]:
        """Discover all valid triangular arbitrage paths.

        A valid triangle is: Quote -> A -> B -> Quote
        where trading pairs exist for all three legs.
        """
        self._symbol_map = self.exchange.get_all_symbols()
        quote = self.settings.quote_currency
        base_currencies = self.settings.base_currencies

        # Build pair lookup: (base, quote) -> symbol
        self._pair_lookup = {}
        for symbol, info in self._symbol_map.items():
            self._pair_lookup[(info.base_asset, info.quote_asset)] = symbol

        # Collect all assets that have a pair with the quote currency
        quote_pairs = set()
        for (base, q), sym in self._pair_lookup.items():
            if q == quote:
                quote_pairs.add(base)
            elif base == quote:
                quote_pairs.add(q)

        triangles = []
        checked = set()

        # For each pair of assets A, B that both have pairs with Quote
        for a in quote_pairs:
            for b in quote_pairs:
                if a == b or a == quote or b == quote:
                    continue

                key = tuple(sorted([a, b]))
                if key in checked:
                    continue
                checked.add(key)

                # Check if A-B pair exists
                if (a, b) in self._pair_lookup or (b, a) in self._pair_lookup:
                    # Check A-Quote pair
                    if (a, quote) in self._pair_lookup or (quote, a) in self._pair_lookup:
                        # Check B-Quote pair
                        if (b, quote) in self._pair_lookup or (quote, b) in self._pair_lookup:
                            triangles.append((quote, a, b))

        self._triangles = triangles
        logger.info(f"Discovered {len(triangles)} triangular paths")

        # Log some examples
        for t in triangles[:5]:
            logger.debug(f"  Triangle: {t[0]} -> {t[1]} -> {t[2]} -> {t[0]}")
        if len(triangles) > 5:
            logger.debug(f"  ... and {len(triangles) - 5} more")

        return triangles

    def _get_symbol_for_pair(self, asset_a: str, asset_b: str) -> Optional[tuple[str, str]]:
        """Find the symbol and direction for trading between two assets.

        Returns (symbol, direction) where direction is:
          'BUY' if we need to buy base (asset_a is quote, asset_b is base)
          'SELL' if we need to sell base (asset_a is base, asset_b is quote)
        """
        # Direct: asset_b is base, asset_a is quote -> asset_b/asset_a
        if (asset_b, asset_a) in self._pair_lookup:
            return self._pair_lookup[(asset_b, asset_a)], "BUY"

        # Reverse: asset_a is base, asset_b is quote -> asset_a/asset_b
        if (asset_a, asset_b) in self._pair_lookup:
            return self._pair_lookup[(asset_a, asset_b)], "SELL"

        return None

    def scan_opportunities(
        self,
        price_book: PriceBook,
        min_profit: Optional[float] = None,
    ) -> list[ArbOpportunity]:
        """Scan all triangles for profitable opportunities.

        Uses bid/ask prices for realistic profit estimation:
        - Buying: use ask price (what sellers are asking)
        - Selling: use bid price (what buyers are bidding)

        Applies fast pre-filter: quick product check to skip obviously
        unprofitable triangles before doing full leg calculation.
        """
        if min_profit is None:
            min_profit = self.settings.min_profit_threshold

        opportunities = []
        input_amount = self.settings.trade_amount_usdt
        fee_mult = self._fee_multiplier

        for triangle in self._triangles:
            try:
                # Fast pre-filter: check if the bid/ask ratio product
                # could possibly yield a profit before computing full legs
                if self._quick_profitable_check(triangle, price_book):
                    opps = self._evaluate_triangle(
                        triangle, price_book, input_amount, fee_mult, min_profit
                    )
                    opportunities.extend(opps)
            except Exception as e:
                logger.debug(f"Error evaluating triangle {triangle}: {e}")

        # Sort by profit ratio descending
        opportunities.sort(key=lambda x: x.profit_ratio, reverse=True)
        return opportunities

    def _quick_profitable_check(
        self,
        triangle: tuple[str, str, str],
        price_book: PriceBook,
    ) -> bool:
        """Quick check if a triangle MIGHT be profitable.

        Uses mid prices to compute the implied conversion rate product.
        If the product is too far below 1.0, skip full evaluation.
        Allows a generous margin (0.5%) to avoid missing opportunities
        that become profitable with bid/ask asymmetry.
        """
        quote, a, b = triangle
        # Check both directions with mid prices
        return (
            self._quick_rate_product([quote, a, b, quote], price_book) or
            self._quick_rate_product([quote, b, a, quote], price_book)
        )

    def _quick_rate_product(
        self,
        path: list[str],
        price_book: PriceBook,
    ) -> bool:
        """Quick mid-price rate product check for a path.
        
        For each leg, compute the conversion rate at mid price.
        If the product of rates * fee_multiplier^3 > 0.995, it's worth
        doing the full bid/ask evaluation.
        """
        product = 1.0
        for i in range(len(path) - 1):
            from_asset = path[i]
            to_asset = path[i + 1]
            result = self._get_symbol_for_pair(from_asset, to_asset)
            if result is None:
                return False
            symbol, side = result
            mid = price_book.get_mid(symbol)
            if mid <= 0:
                return False
            if side == "BUY":
                # Buying base with quote: rate = 1/price (how much base per quote)
                product *= (1.0 / mid)
            else:
                # Selling base for quote: rate = price (how much quote per base)
                product *= mid

        # After fees, net product must be > 1.0 for profit
        # But we use mid (not ask/bid), so allow 0.5% margin for spread asymmetry
        net_product = product * (self._fee_multiplier ** 3)
        return net_product > 0.995  # Allow 0.5% headroom

    def _evaluate_triangle(
        self,
        triangle: tuple[str, str, str],
        price_book: PriceBook,
        input_amount: float,
        fee_mult: float,
        min_profit: float,
    ) -> list[ArbOpportunity]:
        """Evaluate both directions of a triangle for arbitrage.
        
        Direction 1: Quote -> A -> B -> Quote
        Direction 2: Quote -> B -> A -> Quote
        """
        results = []
        quote, a, b = triangle

        # Direction 1: Quote -> A -> B -> Quote
        opp = self._calculate_path(
            [quote, a, b, quote], price_book, input_amount, fee_mult
        )
        if opp and opp.profit_ratio >= min_profit:
            results.append(opp)

        # Direction 2: Quote -> B -> A -> Quote
        opp = self._calculate_path(
            [quote, b, a, quote], price_book, input_amount, fee_mult
        )
        if opp and opp.profit_ratio >= min_profit:
            results.append(opp)

        return results

    def _calculate_path(
        self,
        path: list[str],
        price_book: PriceBook,
        input_amount: float,
        fee_mult: float,
    ) -> Optional[ArbOpportunity]:
        """Calculate profit for a specific path through three trades.

        path: [Quote, A, B, Quote] representing the currency flow.
        """
        current_amount = input_amount
        legs = []

        for i in range(len(path) - 1):
            from_asset = path[i]
            to_asset = path[i + 1]

            result = self._get_symbol_for_pair(from_asset, to_asset)
            if result is None:
                return None

            symbol, side = result
            price_data = price_book.get(symbol)
            if price_data is None:
                return None

            symbol_info = self.exchange.get_symbol_info(symbol)
            if symbol_info is None:
                return None

            if side == "BUY":
                # We're buying base with quote
                # Use ask price (what we pay)
                price = price_data["ask"]
                if price <= 0:
                    return None
                quantity = current_amount / price
                # Check available liquidity
                available_qty = price_data.get("ask_qty", 0)
                if available_qty > 0 and quantity > available_qty:
                    quantity = available_qty  # Cap at available liquidity
                current_amount = quantity * fee_mult
            else:
                # We're selling base for quote
                # Use bid price (what we receive)
                price = price_data["bid"]
                if price <= 0:
                    return None
                quantity = current_amount
                # Check available liquidity
                available_qty = price_data.get("bid_qty", 0)
                if available_qty > 0 and quantity > available_qty:
                    quantity = available_qty
                current_amount = quantity * price * fee_mult

            legs.append(ArbLeg(
                symbol=symbol,
                side=side,
                price=price,
                quantity=quantity,
                base_asset=symbol_info.base_asset,
                quote_asset=symbol_info.quote_asset,
            ))

        # Calculate profit
        final_amount = current_amount
        profit_ratio = (final_amount - input_amount) / input_amount
        profit_usdt = final_amount - input_amount

        return ArbOpportunity(
            triangle=(path[0], path[1], path[2]),
            legs=legs,
            profit_ratio=profit_ratio,
            profit_usdt=profit_usdt,
            input_amount=input_amount,
        )

    def get_watched_symbols(self) -> list[str]:
        """Get all symbols needed for the discovered triangles."""
        symbols = set()
        for triangle in self._triangles:
            quote, a, b = triangle
            # All three legs
            for from_a, to_a in [(quote, a), (a, b), (b, quote)]:
                result = self._get_symbol_for_pair(from_a, to_a)
                if result:
                    symbols.add(result[0])
            # Reverse direction
            for from_a, to_a in [(quote, b), (b, a), (a, quote)]:
                result = self._get_symbol_for_pair(from_a, to_a)
                if result:
                    symbols.add(result[0])
        return list(symbols)
