#!/usr/bin/env python3
"""Offline Simulation Runner - Runs the arbitrage bot with simulated market data.

No API keys required. Demonstrates the full scanning and execution pipeline
with realistic price movements and periodically injected arbitrage opportunities.

Usage:
    python3 simulate.py
    python3 simulate.py --amount 200 --threshold 0.001 --duration 120
"""

import argparse
import asyncio
import signal
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Set dummy env vars so settings don't complain
os.environ.setdefault("BINANCE_API_KEY", "simulation_mode")
os.environ.setdefault("BINANCE_API_SECRET", "simulation_mode")
os.environ.setdefault("BINANCE_TESTNET", "true")

from config.settings import get_settings
from core.simulator import SimulatedExchange
from core.price_stream import PriceBook
from core.executor import OrderExecutor, ExecutionResult, ExecutionStatus
from core.risk_manager import RiskManager
from strategies.triangular_arb import TriangleScanner, ArbOpportunity
from strategies.spread_arb import SpreadScanner
from utils.logger import setup_logger, get_logger
from utils.helpers import Timer

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.live import Live
from rich.columns import Columns
from rich.rule import Rule

logger = get_logger("simulation")
console = Console()


class SimulationStats:
    """Track simulation statistics."""

    def __init__(self):
        self.start_time = time.time()
        self.scan_count = 0
        self.opportunities_found = 0
        self.trades_executed = 0
        self.trades_successful = 0
        self.total_profit = 0.0
        self.best_profit = 0.0
        self.worst_profit = 0.0
        self.total_fees = 0.0
        self.recent_opps: list[ArbOpportunity] = []
        self.recent_trades: list[dict] = []
        self.pnl_history: list[float] = []


def render_dashboard(
    stats: SimulationStats,
    exchange: SimulatedExchange,
    risk_status: dict,
    settings,
) -> str:
    """Render the full dashboard."""
    uptime = time.time() - stats.start_time
    hours, remainder = divmod(int(uptime), 3600)
    minutes, seconds = divmod(remainder, 60)

    # --- Header ---
    console.print(Rule("[bold cyan]Binance Arbitrage Bot - Simulation Mode[/bold cyan]"))
    console.print()

    # --- Status Panel ---
    status_table = Table(show_header=False, box=None, padding=(0, 2))
    status_table.add_column("Key", style="dim")
    status_table.add_column("Value")

    status_table.add_row("Mode", "[yellow]SIMULATION (Offline)[/yellow]")
    status_table.add_row("Uptime", f"{hours:02d}:{minutes:02d}:{seconds:02d}")
    status_table.add_row("Scans", f"{stats.scan_count:,}")
    status_table.add_row("", "")

    balance = exchange.get_balance("USDT")
    initial = exchange.initial_balance
    pnl = balance - initial
    pnl_pct = (pnl / initial * 100) if initial > 0 else 0
    pnl_color = "green" if pnl >= 0 else "red"

    status_table.add_row("Initial Balance", f"${initial:,.2f} USDT")
    status_table.add_row("Current Balance", f"[bold]${balance:,.2f} USDT[/bold]")
    status_table.add_row("P&L", f"[{pnl_color}]${pnl:+,.4f} ({pnl_pct:+.4f}%)[/{pnl_color}]")
    status_table.add_row("", "")
    status_table.add_row("Trade Amount", f"${settings.trade_amount_usdt}")
    status_table.add_row("Min Profit", f"{settings.min_profit_threshold * 100:.3f}%")
    status_table.add_row("", "")

    win_rate = (stats.trades_successful / stats.trades_executed * 100) if stats.trades_executed > 0 else 0
    status_table.add_row("Trades Executed", str(stats.trades_executed))
    status_table.add_row("Win Rate", f"{win_rate:.1f}%")
    status_table.add_row("Best Trade", f"[green]${stats.best_profit:+.4f}[/green]")
    status_table.add_row("Worst Trade", f"[red]${stats.worst_profit:+.4f}[/red]")
    status_table.add_row("Opps Found", f"{stats.opportunities_found:,}")

    console.print(Panel(status_table, title="[bold]Bot Status[/bold]", border_style="cyan"))

    # --- Top Opportunities ---
    opp_table = Table(title="Top Arbitrage Opportunities")
    opp_table.add_column("#", style="dim", width=3)
    opp_table.add_column("Path", style="cyan", min_width=30)
    opp_table.add_column("Profit %", justify="right", width=12)
    opp_table.add_column("Est. Profit $", justify="right", width=14)

    if not stats.recent_opps:
        opp_table.add_row("-", "Scanning...", "-", "-")
    else:
        for i, opp in enumerate(stats.recent_opps[:8], 1):
            path = " → ".join(opp.triangle) + f" → {opp.triangle[0]}"
            color = "green" if opp.profit_ratio > 0 else "red"
            opp_table.add_row(
                str(i),
                path,
                f"[{color}]{opp.profit_pct:+.4f}%[/{color}]",
                f"[{color}]${opp.profit_usdt:+.4f}[/{color}]",
            )

    console.print(opp_table)

    # --- Recent Trades ---
    if stats.recent_trades:
        trade_table = Table(title="Recent Trades")
        trade_table.add_column("Time", style="dim", width=10)
        trade_table.add_column("Path", style="cyan", min_width=28)
        trade_table.add_column("Status", width=10)
        trade_table.add_column("Profit", justify="right", width=14)
        trade_table.add_column("Exec (ms)", justify="right", width=10)

        for trade in stats.recent_trades[-8:]:
            color = "green" if trade["profit"] > 0 else "red"
            status_text = f"[green]✓[/green]" if trade["success"] else f"[red]✗[/red]"
            trade_table.add_row(
                trade["time"],
                trade["path"],
                status_text,
                f"[{color}]${trade['profit']:+.4f}[/{color}]",
                f"{trade['exec_ms']:.0f}",
            )

        console.print(trade_table)

    # --- Balances ---
    balances = exchange.get_all_balances()
    if len(balances) > 1:
        bal_parts = []
        for asset, amount in sorted(balances.items()):
            if amount > 0.00001:
                bal_parts.append(f"{asset}: {amount:.6f}")
        if bal_parts:
            console.print(
                Panel(
                    "  |  ".join(bal_parts),
                    title="[dim]Asset Balances[/dim]",
                    border_style="dim",
                )
            )

    console.print()
    console.print("[dim]Press Ctrl+C to stop simulation[/dim]")


async def run_simulation(
    duration: int = 0,
    trade_amount: float = 100.0,
    min_threshold: float = 0.001,
    scan_interval: float = 1.0,
):
    """Run the full simulation loop."""
    settings = get_settings()
    settings.trade_amount_usdt = trade_amount
    settings.min_profit_threshold = min_threshold

    console.print(Rule("[bold cyan]Starting Binance Arbitrage Simulation[/bold cyan]"))
    console.print()
    console.print(f"  Trade Amount:    [green]${trade_amount}[/green] USDT")
    console.print(f"  Min Threshold:   [green]{min_threshold * 100:.3f}%[/green]")
    console.print(f"  Scan Interval:   [green]{scan_interval}s[/green]")
    console.print(f"  Duration:        [green]{'unlimited' if duration == 0 else f'{duration}s'}[/green]")
    console.print()

    # Initialize simulated exchange
    exchange = SimulatedExchange(initial_balance=10000.0)
    await exchange.initialize()

    # Initialize scanner
    scanner = TriangleScanner(exchange)
    scanner.set_bnb_fee(True)  # BNB available in simulation
    triangles = scanner.discover_triangles()

    console.print(f"  Discovered [cyan]{len(triangles)}[/cyan] triangular arbitrage paths")
    console.print()

    # Initialize risk manager (with mock exchange)
    risk_mgr = RiskManager(exchange)
    risk_mgr._current_balance = exchange.get_balance("USDT")
    risk_mgr._initial_balance = exchange.initial_balance
    risk_mgr._peak_balance = exchange.initial_balance
    risk_mgr._last_balance_update = time.monotonic()

    # Initialize executor
    executor = OrderExecutor(exchange)

    # Price book
    price_book = PriceBook()

    # Stats
    stats = SimulationStats()

    # Main simulation loop
    running = True
    start = time.time()

    def stop_handler():
        nonlocal running
        running = False

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_handler)

    console.print("[green]Simulation started![/green]")
    console.print()

    try:
        while running:
            if duration > 0 and (time.time() - start) >= duration:
                console.print("\n[yellow]Duration reached, stopping...[/yellow]")
                break

            # 1. Update simulated prices
            tickers = await exchange.get_all_orderbook_tickers()
            for symbol, data in tickers.items():
                await price_book.update(
                    symbol, data["bid"], data["ask"],
                    data["bid_qty"], data["ask_qty"],
                )

            # 2. Scan for opportunities
            timer = Timer("scan")
            timer.__enter__()
            opportunities = scanner.scan_opportunities(price_book)
            timer.__exit__(None, None, None)

            stats.scan_count += 1
            stats.recent_opps = opportunities[:10]

            if opportunities:
                stats.opportunities_found += len(opportunities)
                best = opportunities[0]

                # 3. Execute best opportunity if profitable enough
                if best.profit_ratio >= min_threshold:
                    # Check risk
                    can_trade, reason = await risk_mgr.can_trade(trade_amount)

                    if can_trade:
                        # Execute!
                        result = await executor.execute_arbitrage(
                            best, dry_run=False, price_book=price_book
                        )

                        stats.trades_executed += 1

                        # Calculate actual P&L from balance change
                        new_balance = exchange.get_balance("USDT")
                        trade_pnl = new_balance - risk_mgr._current_balance

                        # Update risk manager balance
                        risk_mgr._current_balance = new_balance
                        if new_balance > risk_mgr._peak_balance:
                            risk_mgr._peak_balance = new_balance
                        risk_mgr.record_trade(trade_pnl)

                        if result.success:
                            stats.trades_successful += 1
                            result.actual_profit_usdt = trade_pnl

                        stats.total_profit += trade_pnl

                        if trade_pnl > stats.best_profit:
                            stats.best_profit = trade_pnl
                        if trade_pnl < stats.worst_profit:
                            stats.worst_profit = trade_pnl

                        path = " → ".join(best.triangle) + f" → {best.triangle[0]}"
                        stats.recent_trades.append({
                            "time": time.strftime("%H:%M:%S"),
                            "path": path,
                            "success": result.success,
                            "profit": trade_pnl,
                            "exec_ms": result.execution_time_ms,
                        })

                        # Keep only last 20 trades
                        if len(stats.recent_trades) > 20:
                            stats.recent_trades = stats.recent_trades[-20:]

            # 4. Render dashboard
            console.clear()
            render_dashboard(stats, exchange, risk_mgr.status, settings)

            await asyncio.sleep(scan_interval)

    except asyncio.CancelledError:
        pass

    # Final summary
    console.print()
    console.print(Rule("[bold cyan]Simulation Complete[/bold cyan]"))
    console.print()

    elapsed = time.time() - start
    final_balance = exchange.get_balance("USDT")
    total_return = final_balance - exchange.initial_balance
    return_pct = (total_return / exchange.initial_balance * 100)

    summary = Table(show_header=False, box=None, padding=(0, 2))
    summary.add_column("", style="cyan")
    summary.add_column("")

    summary.add_row("Duration", f"{int(elapsed)}s")
    summary.add_row("Total Scans", f"{stats.scan_count:,}")
    summary.add_row("Opportunities Found", f"{stats.opportunities_found:,}")
    summary.add_row("Trades Executed", str(stats.trades_executed))
    summary.add_row("Successful Trades", str(stats.trades_successful))
    win_rate = (stats.trades_successful / stats.trades_executed * 100) if stats.trades_executed > 0 else 0
    summary.add_row("Win Rate", f"{win_rate:.1f}%")
    summary.add_row("", "")
    summary.add_row("Initial Balance", f"${exchange.initial_balance:,.2f}")
    summary.add_row("Final Balance", f"${final_balance:,.2f}")

    ret_color = "green" if total_return >= 0 else "red"
    summary.add_row("Total Return", f"[{ret_color}]${total_return:+,.4f} ({return_pct:+.4f}%)[/{ret_color}]")
    summary.add_row("Best Single Trade", f"[green]${stats.best_profit:+.4f}[/green]")
    summary.add_row("Worst Single Trade", f"[red]${stats.worst_profit:+.4f}[/red]")

    console.print(Panel(summary, title="[bold]Final Results[/bold]", border_style="green"))
    console.print()

    await exchange.close()


def main():
    parser = argparse.ArgumentParser(description="Run arbitrage bot simulation")
    parser.add_argument("--amount", type=float, default=100.0, help="Trade amount in USDT (default: 100)")
    parser.add_argument("--threshold", type=float, default=0.001, help="Min profit threshold (default: 0.001 = 0.1%%)")
    parser.add_argument("--duration", type=int, default=0, help="Duration in seconds (0 = unlimited)")
    parser.add_argument("--interval", type=float, default=1.0, help="Scan interval in seconds (default: 1.0)")
    args = parser.parse_args()

    setup_logger()

    asyncio.run(run_simulation(
        duration=args.duration,
        trade_amount=args.amount,
        min_threshold=args.threshold,
        scan_interval=args.interval,
    ))


if __name__ == "__main__":
    main()
