"""Performance Dashboard - Real-time console display of bot status."""

import asyncio
import time
from typing import Optional

from rich.console import Console
from rich.table import Table
from rich.layout import Layout
from rich.panel import Panel
from rich.live import Live
from rich.text import Text

from core.executor import OrderExecutor
from core.risk_manager import RiskManager
from core.price_stream import PriceBook
from strategies.triangular_arb import ArbOpportunity


class Dashboard:
    """Rich console dashboard for monitoring bot performance."""

    def __init__(
        self,
        executor: OrderExecutor,
        risk_manager: RiskManager,
    ):
        self.executor = executor
        self.risk_manager = risk_manager
        self.console = Console()
        self._recent_opportunities: list[ArbOpportunity] = []
        self._last_scan_time: float = 0
        self._scan_count: int = 0
        self._start_time: float = time.time()

    def update_opportunities(self, opportunities: list[ArbOpportunity]):
        """Update the list of recent opportunities."""
        self._recent_opportunities = opportunities[:10]
        self._last_scan_time = time.time()
        self._scan_count += 1

    def render(self) -> Table:
        """Render the dashboard as a rich table."""
        # Main status table
        status_table = Table(title="🤖 Binance Arbitrage Bot", show_header=False)
        status_table.add_column("Key", style="cyan")
        status_table.add_column("Value", style="green")

        # Uptime
        uptime = time.time() - self._start_time
        hours, remainder = divmod(int(uptime), 3600)
        minutes, seconds = divmod(remainder, 60)
        status_table.add_row("Uptime", f"{hours:02d}:{minutes:02d}:{seconds:02d}")
        status_table.add_row("Scans", str(self._scan_count))

        # Risk status
        risk = self.risk_manager.status
        status_table.add_row("", "")
        status_table.add_row(
            "Status",
            Text("⛔ HALTED" if risk["is_halted"] else "✅ ACTIVE",
                 style="red" if risk["is_halted"] else "green"),
        )
        status_table.add_row(
            "Balance", f"${risk['current_balance']:.2f} USDT"
        )
        status_table.add_row(
            "Drawdown", f"{risk['current_drawdown_pct']:.2f}%"
        )
        status_table.add_row(
            "Daily P&L",
            Text(
                f"${risk['daily_pnl']:+.4f}",
                style="green" if risk["daily_pnl"] >= 0 else "red",
            ),
        )
        status_table.add_row("Daily Trades", str(risk["daily_trades"]))

        # Execution stats
        stats = self.executor.stats
        status_table.add_row("", "")
        status_table.add_row("Total Trades", str(stats["total_trades"]))
        status_table.add_row(
            "Success Rate", f"{stats['success_rate']:.1f}%"
        )
        status_table.add_row(
            "Total Profit",
            Text(
                f"${stats['total_profit_usdt']:+.4f}",
                style="green" if stats["total_profit_usdt"] >= 0 else "red",
            ),
        )

        return status_table

    def render_opportunities(self) -> Table:
        """Render the opportunities table."""
        opp_table = Table(title="📊 Top Arbitrage Opportunities")
        opp_table.add_column("#", style="dim")
        opp_table.add_column("Path", style="cyan")
        opp_table.add_column("Profit %", justify="right")
        opp_table.add_column("Profit $", justify="right")
        opp_table.add_column("Legs", style="dim")

        if not self._recent_opportunities:
            opp_table.add_row("", "No opportunities found", "", "", "")
        else:
            for i, opp in enumerate(self._recent_opportunities[:10], 1):
                path = " → ".join(opp.triangle) + f" → {opp.triangle[0]}"
                profit_style = "green" if opp.profit_ratio > 0 else "red"
                legs_str = " | ".join(
                    f"{l.side[0]} {l.symbol}" for l in opp.legs
                )
                opp_table.add_row(
                    str(i),
                    path,
                    Text(f"{opp.profit_pct:+.4f}%", style=profit_style),
                    Text(f"${opp.profit_usdt:+.4f}", style=profit_style),
                    legs_str,
                )

        return opp_table

    def print_status(self):
        """Print a single status update to console."""
        self.console.clear()
        self.console.print(self.render())
        self.console.print()
        self.console.print(self.render_opportunities())

        # Recent executions
        history = self.executor.history[-5:]
        if history:
            exec_table = Table(title="📋 Recent Executions")
            exec_table.add_column("Time", style="dim")
            exec_table.add_column("Status")
            exec_table.add_column("Profit", justify="right")
            exec_table.add_column("Time (ms)", justify="right")

            for result in reversed(history):
                t = time.strftime(
                    "%H:%M:%S", time.localtime(result.timestamp)
                )
                status_style = (
                    "green" if result.success else "red"
                )
                exec_table.add_row(
                    t,
                    Text(result.status.value, style=status_style),
                    f"${result.actual_profit_usdt:+.4f}",
                    f"{result.execution_time_ms:.0f}",
                )

            self.console.print()
            self.console.print(exec_table)
