"""Order Execution Engine - Handles fast sequential order execution with adaptive sizing.

Key optimizations:
- Adaptive leg quantity: uses actual fill from leg N to compute leg N+1 quantity
- Accurate profit calculation from real fill prices
- Price staleness validation before execution
- Per-opportunity locking (allows concurrent independent trades)
"""

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from core.exchange import ExchangeClient
from strategies.triangular_arb import ArbOpportunity, ArbLeg
from utils.logger import get_logger
from utils.helpers import Timer, round_step_size

logger = get_logger("executor")


class ExecutionStatus(Enum):
    PENDING = "pending"
    EXECUTING = "executing"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class ExecutionResult:
    """Result of executing an arbitrage opportunity."""
    opportunity: ArbOpportunity
    status: ExecutionStatus
    legs_executed: int = 0
    total_legs: int = 0
    actual_profit_usdt: float = 0.0
    execution_time_ms: float = 0.0
    error: Optional[str] = None
    orders: list[dict] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    @property
    def success(self) -> bool:
        return self.status == ExecutionStatus.COMPLETED


# Maximum execution history entries to keep in memory
MAX_HISTORY_SIZE = 1000

# Maximum concurrent executions
MAX_CONCURRENT_EXECUTIONS = 3


class OrderExecutor:
    """Executes arbitrage trades with adaptive sizing and speed optimization."""

    def __init__(self, exchange: ExchangeClient):
        self.exchange = exchange
        self._execution_semaphore = asyncio.Semaphore(MAX_CONCURRENT_EXECUTIONS)
        self._active_symbols: set[str] = set()  # Track symbols in active trades
        self._symbol_lock = asyncio.Lock()
        self._active_executions: int = 0
        self._execution_history: list[ExecutionResult] = []
        self._total_profit: float = 0.0
        self._total_trades: int = 0
        self._successful_trades: int = 0

    async def can_execute(self, opportunity: ArbOpportunity) -> bool:
        """Check if any symbols in this opportunity are currently being traded.
        
        Prevents conflicting trades on the same symbols while allowing
        concurrent execution of trades on independent symbols.
        """
        opp_symbols = {leg.symbol for leg in opportunity.legs}
        async with self._symbol_lock:
            if opp_symbols & self._active_symbols:
                return False
            return True

    async def execute_arbitrage(
        self,
        opportunity: ArbOpportunity,
        dry_run: bool = False,
        price_book=None,
    ) -> ExecutionResult:
        """Execute a triangular arbitrage opportunity.

        Uses adaptive leg sizing: each leg's quantity is computed from
        the actual fill of the previous leg, not pre-calculated estimates.
        Validates price freshness before execution if price_book is provided.

        Args:
            opportunity: The arbitrage opportunity to execute
            dry_run: If True, simulate without placing real orders
            price_book: Optional PriceBook for staleness validation

        Returns:
            ExecutionResult with details of the execution
        """
        # Check price staleness before acquiring the semaphore
        if price_book is not None:
            symbols = [leg.symbol for leg in opportunity.legs]
            if not price_book.are_prices_fresh(symbols, max_age=3.0):
                stale = [s for s in symbols if price_book.is_stale(s, 3.0)]
                logger.warning(f"Skipping execution: stale prices for {stale}")
                return ExecutionResult(
                    opportunity=opportunity,
                    status=ExecutionStatus.CANCELLED,
                    total_legs=len(opportunity.legs),
                    error=f"Stale prices: {stale}",
                )

        # Reserve symbols to prevent conflicting concurrent trades
        opp_symbols = {leg.symbol for leg in opportunity.legs}
        async with self._symbol_lock:
            if opp_symbols & self._active_symbols:
                return ExecutionResult(
                    opportunity=opportunity,
                    status=ExecutionStatus.CANCELLED,
                    total_legs=len(opportunity.legs),
                    error="Symbol conflict with active trade",
                )
            self._active_symbols.update(opp_symbols)

        try:
            async with self._execution_semaphore:
                return await self._execute(opportunity, dry_run)
        finally:
            async with self._symbol_lock:
                self._active_symbols -= opp_symbols

    async def _execute(
        self,
        opp: ArbOpportunity,
        dry_run: bool,
    ) -> ExecutionResult:
        """Internal execution logic with adaptive leg sizing."""
        result = ExecutionResult(
            opportunity=opp,
            status=ExecutionStatus.EXECUTING,
            total_legs=len(opp.legs),
        )

        timer = Timer("execution")
        timer.__enter__()

        try:
            logger.info(f"{'[DRY RUN] ' if dry_run else ''}Executing: {opp}")
            self._active_executions += 1

            # Track the actual amount flowing through the trade
            current_amount = opp.input_amount  # Start with USDT input

            for i, leg in enumerate(opp.legs):
                leg_num = i + 1

                # Adaptive quantity: use actual amount from previous leg
                if i == 0:
                    quantity = leg.quantity  # First leg uses planned quantity
                else:
                    # Compute quantity from actual fill of previous leg
                    prev_order = result.orders[-1]
                    prev_leg = opp.legs[i - 1]
                    
                    if prev_leg.side == "BUY":
                        # We bought base asset. After fill we received:
                        # quantity - commission (commission in base asset)
                        actual_received = prev_order["quantity"] - prev_order.get("commission", 0)
                        if leg.side == "SELL":
                            # We have base, now selling it
                            quantity = actual_received
                        else:
                            # We have base, using as quote to buy next base
                            quantity = actual_received / leg.price if leg.price > 0 else 0
                    else:
                        # We sold base for quote. After fill we received:
                        # quantity * price - commission (commission in quote asset)
                        actual_received = (prev_order["quantity"] * prev_order["price"]
                                          - prev_order.get("commission", 0))
                        if leg.side == "BUY":
                            # We have quote, buying base with it
                            quantity = actual_received / leg.price if leg.price > 0 else 0
                        else:
                            # We have quote, selling it (unusual in triangular arb)
                            quantity = actual_received

                # Validate quantity against symbol constraints
                symbol_info = self.exchange.get_symbol_info(leg.symbol)
                if symbol_info and hasattr(symbol_info, 'step_size'):
                    quantity = round_step_size(quantity, symbol_info.step_size)

                logger.info(
                    f"  Leg {leg_num}/{len(opp.legs)}: {leg.side} {leg.symbol} "
                    f"qty={quantity:.8f} price={leg.price:.8f}"
                )

                if dry_run:
                    # Simulate execution with realistic commission
                    # BUY: commission in base (received), SELL: commission in quote (received)
                    if leg.side == "BUY":
                        commission = quantity * 0.00075  # base asset
                    else:
                        commission = quantity * leg.price * 0.00075  # quote asset
                    order = {
                        "orderId": f"dry_run_{i}",
                        "symbol": leg.symbol,
                        "side": leg.side,
                        "price": leg.price,
                        "quantity": quantity,
                        "commission": commission,
                        "status": "FILLED",
                    }
                else:
                    try:
                        order = await self.exchange.place_market_order(
                            symbol=leg.symbol,
                            side=leg.side,
                            quantity=quantity,
                        )
                    except Exception as e:
                        logger.error(f"  Leg {leg_num} FAILED: {e}")
                        result.status = ExecutionStatus.PARTIAL
                        result.error = f"Leg {leg_num} failed: {str(e)}"

                        # Attempt to unwind previous legs
                        if result.legs_executed > 0 and not dry_run:
                            await self._unwind(opp, result.orders)

                        break

                result.orders.append(order)
                result.legs_executed += 1

                # Track actual cost of first leg for profit calculation
                if i == 0 and leg.side == "BUY":
                    first_leg_cost = order["quantity"] * order["price"]

                logger.info(f"  Leg {leg_num} FILLED @ {order.get('price', 0):.8f}")

            if result.legs_executed == len(opp.legs):
                result.status = ExecutionStatus.COMPLETED
                result.actual_profit_usdt = self._calculate_actual_profit(result)
                self._successful_trades += 1
                self._total_profit += result.actual_profit_usdt
                logger.info(
                    f"Arbitrage COMPLETED! Profit: ${result.actual_profit_usdt:.4f}"
                )
            elif result.status != ExecutionStatus.PARTIAL:
                result.status = ExecutionStatus.FAILED

        except Exception as e:
            logger.error(f"Execution error: {e}")
            result.status = ExecutionStatus.FAILED
            result.error = str(e)
        finally:
            timer.__exit__(None, None, None)
            result.execution_time_ms = timer.elapsed_ms
            self._active_executions -= 1
            self._total_trades += 1
            self._execution_history.append(result)
            # Prevent unbounded memory growth
            if len(self._execution_history) > MAX_HISTORY_SIZE:
                self._execution_history = self._execution_history[-MAX_HISTORY_SIZE:]
            logger.info(
                f"Execution finished in {result.execution_time_ms:.1f}ms "
                f"- Status: {result.status.value}"
            )

        return result

    async def _unwind(self, opp: ArbOpportunity, executed_orders: list[dict]):
        """Attempt to unwind partially executed legs using actual fill data.
        
        Uses the actual filled quantities from orders (not planned quantities)
        for accurate unwinding.
        """
        logger.warning(
            f"UNWINDING {len(executed_orders)} completed legs for {opp.triangle}"
        )

        try:
            # Reverse the completed legs using actual fill data
            for i in range(len(executed_orders) - 1, -1, -1):
                order = executed_orders[i]
                leg = opp.legs[i]
                reverse_side = "SELL" if leg.side == "BUY" else "BUY"
                # Use actual filled quantity, accounting for commission
                actual_qty = order["quantity"]
                if leg.side == "BUY":
                    # We bought this qty minus commission, that's what we need to sell back
                    actual_qty = order["quantity"] - order.get("commission", 0)

                logger.info(
                    f"  Unwind: {reverse_side} {leg.symbol} qty={actual_qty:.8f}"
                )

                try:
                    await self.exchange.place_market_order(
                        symbol=leg.symbol,
                        side=reverse_side,
                        quantity=actual_qty,
                    )
                    logger.info(f"  Unwind leg {i + 1} successful")
                except Exception as e:
                    logger.error(
                        f"  CRITICAL: Unwind leg {i + 1} FAILED: {e}. "
                        f"Manual intervention required!"
                    )
        except Exception as e:
            logger.error(f"Unwind process failed: {e}")

    def _calculate_actual_profit(self, result: ExecutionResult) -> float:
        """Calculate actual profit from executed order fill prices.
        
        Traces the actual money flow through all legs using fill prices
        and commissions. Commission is always in the received asset:
        - BUY: commission in base (deducted from received quantity)
        - SELL: commission in quote (deducted from received proceeds)
        """
        if not result.orders or len(result.orders) < 2:
            return 0.0

        orders = result.orders
        legs = result.opportunity.legs

        try:
            # Calculate actual USDT spent (first leg)
            first_order = orders[0]
            first_leg = legs[0]
            if first_leg.side == "BUY":
                # We spent USDT to buy base: cost = qty * price
                actual_input = first_order["quantity"] * first_order["price"]
            else:
                # We sold base we already had for USDT
                actual_input = result.opportunity.input_amount
            
            # Calculate actual USDT received (last leg)
            last_order = orders[-1]
            last_leg = legs[-1]
            if last_leg.side == "SELL":
                # We sold base for USDT: received = qty * price - commission
                # Commission is in quote (USDT) for SELL
                actual_output = (last_order["quantity"] * last_order["price"]
                               - last_order.get("commission", 0))
            else:
                # We bought base with USDT (unusual for last leg)
                actual_output = last_order["quantity"] * last_order["price"]

            return actual_output - actual_input

        except (KeyError, IndexError, ZeroDivisionError) as e:
            logger.warning(f"Profit calculation fallback: {e}")
            total_commission = sum(o.get("commission", 0) for o in orders)
            return result.opportunity.profit_usdt - total_commission

    @property
    def stats(self) -> dict:
        """Get execution statistics."""
        total = self._total_trades
        return {
            "total_trades": total,
            "successful": self._successful_trades,
            "failed": total - self._successful_trades,
            "success_rate": (
                self._successful_trades / total * 100 if total > 0 else 0
            ),
            "total_profit_usdt": self._total_profit,
            "active_executions": self._active_executions,
        }

    @property
    def history(self) -> list[ExecutionResult]:
        """Get execution history."""
        return self._execution_history.copy()
