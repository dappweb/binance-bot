"""Main Bot Engine - Orchestrates all components."""

import asyncio
import signal
import time
from datetime import datetime, timezone
from typing import Optional

from config.settings import get_settings
from core.exchange import ExchangeClient
from core.price_stream import PriceStreamManager
from core.executor import OrderExecutor
from core.risk_manager import RiskManager
from strategies.triangular_arb import TriangleScanner
from strategies.spread_arb import SpreadScanner
from utils.logger import setup_logger, get_logger
from utils.dashboard import Dashboard
from utils.helpers import Timer
from utils.health import HealthMonitor
from utils.persistence import StatePersistence

logger = get_logger("engine")

# Long-running intervals (in seconds)
DAILY_RESET_CHECK_INTERVAL = 60       # Check for new day every 60s
EXCHANGE_INFO_REFRESH_INTERVAL = 3600  # Refresh trading rules every 1h
BALANCE_SYNC_INTERVAL = 300            # Reconcile balances every 5min
STATE_SAVE_INTERVAL = 60               # Persist state every 60s


class ArbitrageBot:
    """Main arbitrage trading bot engine."""

    def __init__(self, dry_run: bool = True):
        self.settings = get_settings()
        self.dry_run = dry_run

        # Core components
        self.exchange = ExchangeClient()
        self.price_stream = PriceStreamManager(self.exchange)
        self.executor = OrderExecutor(self.exchange)
        self.risk_manager = RiskManager(self.exchange)

        # Strategies
        self.triangle_scanner = TriangleScanner(self.exchange)
        self.spread_scanner = SpreadScanner(self.exchange)

        # Dashboard
        self.dashboard = Dashboard(self.executor, self.risk_manager)

        # Long-running support
        self.health_monitor = HealthMonitor()
        self.state_persistence = StatePersistence()

        # State
        self._running = False
        self._scan_task: Optional[asyncio.Task] = None
        self._display_task: Optional[asyncio.Task] = None
        self._maintenance_task: Optional[asyncio.Task] = None
        self._current_date: str = ""  # Track current date for daily reset

    async def initialize(self):
        """Initialize all components."""
        logger.info("=" * 60)
        logger.info("  Binance Arbitrage Bot Initializing...")
        logger.info("=" * 60)
        logger.info(f"  Mode: {'DRY RUN (no real trades)' if self.dry_run else 'LIVE TRADING'}")
        logger.info(f"  Testnet: {self.settings.binance_testnet}")
        logger.info(f"  Trade amount: ${self.settings.trade_amount_usdt}")
        logger.info(f"  Min profit threshold: {self.settings.min_profit_threshold * 100}%")
        logger.info("=" * 60)

        # Initialize exchange client
        await self.exchange.initialize()

        # Initialize risk manager
        await self.risk_manager.initialize()

        # Discover arbitrage paths
        logger.info("Discovering arbitrage opportunities...")
        triangles = self.triangle_scanner.discover_triangles()
        spread_pairs = self.spread_scanner.discover_pairs()

        # Check if BNB is available for fee discount
        bnb_balance = self.exchange.get_balance("BNB")
        if bnb_balance > 0:
            self.triangle_scanner.set_bnb_fee(True)
            self.spread_scanner.set_bnb_fee(True)
            logger.info(f"BNB balance: {bnb_balance:.4f} (fee discount enabled)")
        else:
            self.triangle_scanner.set_bnb_fee(False)
            self.spread_scanner.set_bnb_fee(False)
            logger.info("No BNB balance (standard fees apply)")

        # Set up price stream with relevant symbols
        watched = set()
        watched.update(self.triangle_scanner.get_watched_symbols())
        watched.update(self.spread_scanner.get_watched_symbols())
        self.price_stream.watch_symbols(list(watched))
        logger.info(f"Monitoring {len(watched)} trading pairs")

        # Start price stream
        await self.price_stream.start()

        # Wait for initial prices to load
        await asyncio.sleep(2)

        # Set up health monitoring
        self.health_monitor.register_component(
            "price_stream", timeout=30.0,
            recovery_callback=self._recover_price_stream,
        )
        self.health_monitor.register_component(
            "scan_loop", timeout=60.0,
        )
        await self.health_monitor.start()

        # Wire up price stream health heartbeat
        self.price_stream.set_health_callback(
            lambda: self.health_monitor.heartbeat("price_stream")
        )

        # Track current date for daily resets
        self._current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Restore state if available
        saved = self.state_persistence.load()
        if saved:
            self._restore_state(saved)

        logger.info("Initialization complete!")

    async def run(self):
        """Main bot loop."""
        self._running = True

        # Set up signal handlers for graceful shutdown
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self.shutdown()))

        # Start scanning and display tasks
        self._scan_task = asyncio.create_task(self._arbitrage_scan_loop())
        self._display_task = asyncio.create_task(self._display_loop())
        self._maintenance_task = asyncio.create_task(self._maintenance_loop())

        logger.info("Bot is running. Press Ctrl+C to stop.")

        try:
            await asyncio.gather(
                self._scan_task,
                self._display_task,
                self._maintenance_task,
            )
        except asyncio.CancelledError:
            pass

    async def _arbitrage_scan_loop(self):
        """Continuous arbitrage scanning loop."""
        scan_interval = self.settings.scan_interval

        while self._running:
            try:
                timer = Timer("scan")
                timer.__enter__()

                # Check if trading is allowed
                can_trade, reason = await self.risk_manager.can_trade(
                    self.settings.trade_amount_usdt
                )

                # Heartbeat for health monitor
                self.health_monitor.heartbeat("scan_loop")

                if not can_trade:
                    logger.debug(f"Trading blocked: {reason}")
                    await asyncio.sleep(scan_interval * 10)
                    continue

                # Scan for triangular arbitrage
                tri_opportunities = self.triangle_scanner.scan_opportunities(
                    self.price_stream.price_book
                )

                # Scan for spread arbitrage
                spread_opportunities = self.spread_scanner.scan_opportunities(
                    self.price_stream.price_book
                )

                timer.__exit__(None, None, None)

                self.dashboard.update_opportunities(tri_opportunities)

                # Execute profitable triangular arbitrage opportunities
                execution_tasks = []
                if tri_opportunities:
                    best = tri_opportunities[0]
                    logger.info(
                        f"Found {len(tri_opportunities)} triangular arb opportunities "
                        f"(best: {best.profit_pct:+.4f}%, ${best.profit_usdt:+.4f}) "
                        f"[scan: {timer.elapsed_ms:.1f}ms]"
                    )

                    # Execute top opportunities concurrently (if independent)
                    for opp in tri_opportunities[:3]:
                        if opp.profit_ratio >= self.settings.min_profit_threshold:
                            if await self.executor.can_execute(opp):
                                task = asyncio.create_task(
                                    self._execute_and_record(opp)
                                )
                                execution_tasks.append(task)

                # Execute spread arbitrage opportunities
                if spread_opportunities:
                    best_spread = spread_opportunities[0]
                    logger.info(
                        f"Found {len(spread_opportunities)} spread arb opportunities "
                        f"(best: {best_spread.net_profit_pct:+.4f}%)"
                    )
                    # TODO: Create SpreadExecutor for spread-specific execution
                    # For now, spread scanning provides market intelligence

                # Wait for all executions to complete
                if execution_tasks:
                    await asyncio.gather(*execution_tasks, return_exceptions=True)

                await asyncio.sleep(scan_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Scan loop error: {e}", exc_info=True)
                await asyncio.sleep(5.0)

    async def _execute_and_record(self, opportunity):
        """Execute an opportunity and record the result in risk manager."""
        try:
            result = await self.executor.execute_arbitrage(
                opportunity,
                dry_run=self.dry_run,
                price_book=self.price_stream.price_book,
            )
            if result.success:
                self.risk_manager.record_trade(result.actual_profit_usdt)
        except Exception as e:
            logger.error(f"Execution task error: {e}")

    async def _display_loop(self):
        """Periodic dashboard display update."""
        while self._running:
            try:
                self.dashboard.print_status()
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Display error: {e}")
                await asyncio.sleep(10.0)

    async def _maintenance_loop(self):
        """Periodic maintenance tasks for long-term stability.

        Handles:
        - Daily risk counter reset (UTC midnight)
        - Exchange info refresh (trading rules)
        - Balance reconciliation
        - State persistence
        """
        last_exchange_refresh = time.monotonic()
        last_balance_sync = time.monotonic()
        last_state_save = time.monotonic()

        while self._running:
            try:
                now = time.monotonic()

                # --- Daily Reset (UTC midnight) ---
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                if today != self._current_date:
                    logger.info(f"New day detected: {today}. Resetting daily counters.")
                    self.risk_manager.reset_daily()
                    self._current_date = today

                # --- Exchange Info Refresh ---
                if now - last_exchange_refresh > EXCHANGE_INFO_REFRESH_INTERVAL:
                    try:
                        logger.info("Refreshing exchange trading rules...")
                        await self.exchange._load_exchange_info()
                        # Re-discover triangles in case new pairs were listed
                        self.triangle_scanner.discover_triangles()
                        self.spread_scanner.discover_pairs()
                        last_exchange_refresh = now
                        logger.info("Exchange info refreshed successfully")
                    except Exception as e:
                        logger.error(f"Exchange info refresh failed: {e}")

                # --- Balance Reconciliation ---
                if now - last_balance_sync > BALANCE_SYNC_INTERVAL:
                    try:
                        await self.exchange.update_balances()
                        last_balance_sync = now
                        logger.debug("Balance reconciliation complete")
                    except Exception as e:
                        logger.error(f"Balance sync failed: {e}")

                # --- State Persistence ---
                if now - last_state_save > STATE_SAVE_INTERVAL:
                    try:
                        self._save_state()
                        last_state_save = now
                    except Exception as e:
                        logger.error(f"State save failed: {e}")

                await asyncio.sleep(DAILY_RESET_CHECK_INTERVAL)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Maintenance loop error: {e}")
                await asyncio.sleep(60.0)

    async def _recover_price_stream(self):
        """Recovery callback for price stream failures."""
        logger.warning("Attempting price stream recovery...")
        try:
            await self.price_stream.stop()
            await asyncio.sleep(2)
            await self.price_stream.start()
            logger.info("Price stream recovered successfully")
        except Exception as e:
            logger.error(f"Price stream recovery failed: {e}")

    def _save_state(self):
        """Save current bot state to disk."""
        state = {
            "executor_stats": self.executor.stats,
            "risk_status": self.risk_manager.status,
            "current_date": self._current_date,
            "daily_pnl": self.risk_manager._daily_pnl,
            "daily_trades": self.risk_manager._daily_trades,
            "daily_losses": self.risk_manager._daily_losses,
            "total_profit": self.executor._total_profit,
            "total_trades": self.executor._total_trades,
            "successful_trades": self.executor._successful_trades,
            "health": self.health_monitor.status,
        }
        self.state_persistence.save(state)

    def _restore_state(self, state: dict):
        """Restore bot state from saved data."""
        try:
            saved_date = state.get("current_date", "")
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

            # Only restore daily counters if same day
            if saved_date == today:
                self.risk_manager._daily_pnl = state.get("daily_pnl", 0.0)
                self.risk_manager._daily_trades = state.get("daily_trades", 0)
                self.risk_manager._daily_losses = state.get("daily_losses", 0.0)
                logger.info(
                    f"Restored daily state: P&L=${self.risk_manager._daily_pnl:+.4f}, "
                    f"trades={self.risk_manager._daily_trades}"
                )
            else:
                logger.info("Saved state is from a previous day, starting fresh")

            # Restore cumulative counters
            self.executor._total_profit = state.get("total_profit", 0.0)
            self.executor._total_trades = state.get("total_trades", 0)
            self.executor._successful_trades = state.get("successful_trades", 0)

            logger.info(
                f"Restored cumulative stats: profit=${self.executor._total_profit:+.4f}, "
                f"trades={self.executor._total_trades}"
            )
        except Exception as e:
            logger.error(f"State restoration failed: {e}")

    async def shutdown(self):
        """Graceful shutdown."""
        logger.info("Shutting down...")
        self._running = False

        if self._scan_task:
            self._scan_task.cancel()
        if self._display_task:
            self._display_task.cancel()
        if self._maintenance_task:
            self._maintenance_task.cancel()

        # Save final state
        try:
            self._save_state()
        except Exception:
            pass

        await self.health_monitor.stop()
        await self.price_stream.stop()
        await self.exchange.close()

        # Print final stats
        stats = self.executor.stats
        risk = self.risk_manager.status
        logger.info("=" * 60)
        logger.info("  Final Statistics")
        logger.info("=" * 60)
        logger.info(f"  Total trades: {stats['total_trades']}")
        logger.info(f"  Successful: {stats['successful']}")
        logger.info(f"  Success rate: {stats['success_rate']:.1f}%")
        logger.info(f"  Total profit: ${stats['total_profit_usdt']:+.4f}")
        logger.info(f"  Final balance: ${risk['current_balance']:.2f}")
        logger.info("=" * 60)
        logger.info("Bot stopped.")
