#!/usr/bin/env python3
"""Binance Arbitrage Trading Bot - Entry Point.

Usage:
    python main.py                  # Run in dry-run mode (default, safe)
    python main.py --live           # Run in live trading mode
    python main.py --scan-only      # Only scan for opportunities, no execution
    python main.py --help           # Show help

Environment:
    Copy .env.example to .env and configure your API keys.
"""

import argparse
import asyncio
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv

# Load environment variables before importing settings
load_dotenv()

from config.settings import get_settings
from core.engine import ArbitrageBot
from utils.logger import setup_logger


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Binance Arbitrage Trading Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                   Dry run (no real trades, safe to test)
  python main.py --live            Live trading (REAL MONEY!)
  python main.py --scan-only       Just scan and display opportunities
  python main.py --amount 50       Set trade amount to $50 USDT
  python main.py --threshold 0.002 Set min profit threshold to 0.2%
        """,
    )

    parser.add_argument(
        "--live",
        action="store_true",
        default=False,
        help="Enable live trading (default: dry run)",
    )

    parser.add_argument(
        "--scan-only",
        action="store_true",
        default=False,
        help="Only scan for opportunities, do not execute trades",
    )

    parser.add_argument(
        "--amount",
        type=float,
        default=None,
        help="Trade amount in USDT (overrides .env setting)",
    )

    parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="Minimum profit threshold as decimal (e.g., 0.001 = 0.1%%)",
    )

    parser.add_argument(
        "--testnet",
        action="store_true",
        default=None,
        help="Force testnet mode",
    )

    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=None,
        help="Set log level",
    )

    return parser.parse_args()


async def run_scan_only():
    """Run in scan-only mode - just show opportunities."""
    from core.exchange import ExchangeClient
    from core.price_stream import PriceStreamManager
    from strategies.triangular_arb import TriangleScanner
    from strategies.spread_arb import SpreadScanner
    from rich.console import Console
    from rich.table import Table

    console = Console()
    settings = get_settings()

    console.print("[bold cyan]Binance Arbitrage Scanner[/bold cyan]")
    console.print(f"Trade amount: ${settings.trade_amount_usdt}")
    console.print(f"Min profit: {settings.min_profit_threshold * 100}%")
    console.print()

    exchange = ExchangeClient()
    await exchange.initialize()

    # Discover triangles
    scanner = TriangleScanner(exchange)
    triangles = scanner.discover_triangles()
    console.print(f"Found [green]{len(triangles)}[/green] triangular paths")

    # Discover spread pairs
    spread_scanner = SpreadScanner(exchange)
    spread_pairs = spread_scanner.discover_pairs()
    console.print(
        f"Found [green]{len(spread_pairs)}[/green] spread arbitrage assets"
    )

    # Set up price stream
    price_stream = PriceStreamManager(exchange)
    watched = set()
    watched.update(scanner.get_watched_symbols())
    watched.update(spread_scanner.get_watched_symbols())
    price_stream.watch_symbols(list(watched))
    await price_stream.start()

    console.print(f"\nMonitoring [cyan]{len(watched)}[/cyan] pairs...")
    console.print("Waiting for price data...\n")
    await asyncio.sleep(3)

    try:
        scan_count = 0
        while True:
            scan_count += 1

            # Triangular arbitrage scan
            tri_opps = scanner.scan_opportunities(price_stream.price_book)

            # Spread arbitrage scan
            spread_opps = spread_scanner.scan_opportunities(price_stream.price_book)

            console.clear()
            console.print(
                f"[bold cyan]Scan #{scan_count}[/bold cyan] - "
                f"Prices: {price_stream.price_book.count}"
            )

            # Triangular results table
            table = Table(title=f"Triangular Arbitrage ({len(tri_opps)} found)")
            table.add_column("#")
            table.add_column("Path")
            table.add_column("Profit %", justify="right")
            table.add_column("Profit $", justify="right")

            for i, opp in enumerate(tri_opps[:15], 1):
                path = " → ".join(opp.triangle) + f" → {opp.triangle[0]}"
                table.add_row(
                    str(i),
                    path,
                    f"{opp.profit_pct:+.4f}%",
                    f"${opp.profit_usdt:+.4f}",
                )

            console.print(table)

            # Spread results table
            if spread_opps:
                spread_table = Table(
                    title=f"Spread Arbitrage ({len(spread_opps)} found)"
                )
                spread_table.add_column("#")
                spread_table.add_column("Buy")
                spread_table.add_column("Sell")
                spread_table.add_column("Spread %", justify="right")
                spread_table.add_column("Net Profit %", justify="right")

                for i, opp in enumerate(spread_opps[:10], 1):
                    spread_table.add_row(
                        str(i),
                        f"{opp.buy_symbol} @ {opp.buy_price:.6f}",
                        f"{opp.sell_symbol} @ {opp.sell_price:.6f}",
                        f"{opp.spread_pct:+.4f}%",
                        f"{opp.net_profit_pct:+.4f}%",
                    )

                console.print(spread_table)

            console.print("\nPress Ctrl+C to stop")
            await asyncio.sleep(2)

    except (KeyboardInterrupt, asyncio.CancelledError):
        console.print("\nStopping scanner...")
    finally:
        await price_stream.stop()
        await exchange.close()


async def main():
    """Main entry point."""
    args = parse_args()
    settings = get_settings()

    # Override settings from CLI args
    if args.amount is not None:
        settings.trade_amount_usdt = args.amount
    if args.threshold is not None:
        settings.min_profit_threshold = args.threshold
    if args.testnet is not None:
        settings.binance_testnet = args.testnet
    if args.log_level is not None:
        settings.log_level = args.log_level

    # Set up logging
    setup_logger()

    # Validate API keys
    if not settings.binance_api_key or settings.binance_api_key == "your_api_key_here":
        print("ERROR: Please configure your Binance API keys in .env file")
        print("Copy .env.example to .env and fill in your API key and secret")
        sys.exit(1)

    # Scan-only mode
    if args.scan_only:
        await run_scan_only()
        return

    # Trading mode
    dry_run = not args.live

    if args.live:
        print("=" * 60)
        print("  ⚠️  LIVE TRADING MODE - REAL MONEY AT RISK!")
        print("=" * 60)
        print(f"  API Key: {settings.binance_api_key[:8]}...")
        print(f"  Testnet: {settings.binance_testnet}")
        print(f"  Trade Amount: ${settings.trade_amount_usdt}")
        print()

        if not settings.binance_testnet:
            confirm = input("Type 'CONFIRM' to proceed with LIVE trading: ")
            if confirm != "CONFIRM":
                print("Aborted.")
                sys.exit(0)

    # Create and run bot
    bot = ArbitrageBot(dry_run=dry_run)

    try:
        await bot.initialize()
        await bot.run()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"Fatal error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await bot.shutdown()


if __name__ == "__main__":
    # Use uvloop for faster async event loop (2-4x speedup over default)
    try:
        import uvloop
        uvloop.install()
        print("[perf] uvloop installed as event loop policy")
    except ImportError:
        print("[perf] uvloop not available, using default event loop")

    asyncio.run(main())
