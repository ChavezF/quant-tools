#!/usr/bin/env python3.12
"""
quant.py — Unified runner for the quant toolkit.

Subcommands:
  scan        options screener
  risk        portfolio risk dashboard
  earnings    earnings IV-crush scanner
  brief       daily market brief
  all         run all of the above

Examples:
  ./quant.py scan --watchlist SPY QQQ NVDA --strategies csp bull_put
  ./quant.py risk --target-watchlist SPY QQQ NVDA AAPL
  ./quant.py earnings --watchlist NVDA AAPL TSLA
  ./quant.py brief
  ./quant.py all --watchlist SPY QQQ NVDA AAPL MSFT TSLA
"""
import argparse
import os
import sys
import subprocess
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
PY = os.environ.get("QUANT_PYTHON", sys.executable)


def run(script: str, *args: str) -> int:
    cmd = [PY, str(SCRIPT_DIR / script), *args]
    return subprocess.call(cmd)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan", help="Options screener")
    p_scan.add_argument("--watchlist", nargs="+", required=True)
    p_scan.add_argument("--strategies", nargs="+", default=["csp", "cc"])
    p_scan.add_argument("--min-dte", type=int, default=14)
    p_scan.add_argument("--max-dte", type=int, default=45)
    p_scan.add_argument("--target-delta", type=float, default=0.30)

    p_risk = sub.add_parser("risk", help="Portfolio risk")
    p_risk.add_argument("--target-watchlist", nargs="+")

    p_earn = sub.add_parser("earnings", help="Earnings IV scanner")
    p_earn.add_argument("--watchlist", nargs="+", required=True)
    p_earn.add_argument("--days-ahead", type=int, default=90)

    p_iv = sub.add_parser("iv-rank", help="IV rank & percentile")
    p_iv.add_argument("--tickers", nargs="+", required=True)

    p_term = sub.add_parser("term-structure", help="ATM IV term structure")
    p_term.add_argument("--ticker", required=True)
    p_term.add_argument("--max-dte", type=int, default=180)

    p_btest = sub.add_parser("backtest", help="Earnings strangle backtest (v1, circular math — do not trust)")
    p_btest.add_argument("--tickers", nargs="+", required=True)
    p_btest.add_argument("--num-events", type=int, default=8)

    p_btest2 = sub.add_parser("backtest2", help="Earnings strangle backtest v2 (no look-ahead, OOS, Sharpe/Sortino/MaxDD)")
    p_btest2.add_argument("--tickers", nargs="+", required=True)
    p_btest2.add_argument("--num-events", type=int, default=12)
    p_btest2.add_argument("--strategies", nargs="+", default=["short_strangle", "iron_condor"])
    p_btest2.add_argument("--oos", action="store_true")
    p_btest2.add_argument("--portfolio", action="store_true")

    p_mc = sub.add_parser("monte-carlo", help="Monte Carlo stress test for 16Δ strangle")
    p_mc.add_argument("--ticker", required=True)
    p_mc.add_argument("--num-simulations", type=int, default=10000)
    p_mc.add_argument("--hold-days", type=int, default=5)
    p_mc.add_argument("--method", choices=["parametric", "bootstrap", "both"], default="both")
    p_mc.add_argument("--tail-events", action="store_true")

    p_macro = sub.add_parser("macro", help="Macro regime overlay")
    p_macro.add_argument("--watchlist", nargs="+")
    p_macro.add_argument("--days", type=int, default=7)

    p_pos = sub.add_parser("positions", help="Position tracker with Greeks")
    p_pos.add_argument("--init", action="store_true", help="Reset state, mark current as entry")
    p_pos.add_argument("--json", action="store_true")

    p_brief = sub.add_parser("brief", help="Daily market brief")
    p_brief.add_argument("--watchlist", nargs="+")
    p_brief.add_argument("--send", action="store_true")
    p_brief.add_argument("--dry-run", action="store_true")

    p_all = sub.add_parser("all", help="Run all of the above")
    p_all.add_argument("--watchlist", nargs="+", required=True)
    p_all.add_argument("--strategies", nargs="+", default=["csp", "cc"])
    p_all.add_argument("--min-dte", type=int, default=14)
    p_all.add_argument("--max-dte", type=int, default=45)
    p_all.add_argument("--target-delta", type=float, default=0.30)
    p_all.add_argument("--days-ahead", type=int, default=90)
    p_all.add_argument("--num-events", type=int, default=8)

    args = ap.parse_args()

    if args.cmd == "scan":
        return run("options_screener.py",
                   "--watchlist", *args.watchlist,
                   "--strategies", *args.strategies,
                   "--min-dte", str(args.min_dte),
                   "--max-dte", str(args.max_dte),
                   "--target-delta", str(args.target_delta))
    elif args.cmd == "risk":
        cmd = ["portfolio_risk.py"]
        if args.target_watchlist:
            cmd += ["--target-watchlist", *args.target_watchlist]
        return run(*cmd)
    elif args.cmd == "earnings":
        return run("earnings_iv_scanner.py",
                   "--watchlist", *args.watchlist,
                   "--days-ahead", str(args.days_ahead))
    elif args.cmd == "iv-rank":
        return run("iv_rank.py", "--tickers", *args.tickers)
    elif args.cmd == "term-structure":
        return run("term_structure.py", "--ticker", args.ticker, "--max-dte", str(args.max_dte))
    elif args.cmd == "backtest":
        return run("earnings_backtest.py", "--tickers", *args.tickers,
                   "--num-events", str(args.num_events))
    elif args.cmd == "backtest2":
        cmd = ["earnings_backtest_v2.py", "--tickers", *args.tickers,
               "--num-events", str(args.num_events),
               "--strategies", *args.strategies]
        if args.oos:
            cmd += ["--oos"]
        if args.portfolio:
            cmd += ["--portfolio"]
        return run(*cmd)
    elif args.cmd == "monte-carlo":
        cmd = ["monte_carlo.py", "--ticker", args.ticker,
               "--num-simulations", str(args.num_simulations),
               "--hold-days", str(args.hold_days),
               "--method", args.method]
        if args.tail_events:
            cmd += ["--tail-events"]
        return run(*cmd)
    elif args.cmd == "macro":
        cmd = ["macro_overlay.py"]
        if args.watchlist:
            cmd += ["--watchlist", *args.watchlist]
        cmd += ["--days", str(args.days)]
        return run(*cmd)
    elif args.cmd == "positions":
        cmd = ["position_tracker.py"]
        if args.init:
            cmd += ["--init"]
        if args.json:
            cmd += ["--json"]
        return run(*cmd)
    elif args.cmd == "brief":
        cmd = ["daily_brief.py"]
        if args.watchlist:
            cmd += ["--watchlist", *args.watchlist]
        if args.send:
            cmd += ["--send"]
        if args.dry_run:
            cmd += ["--dry-run"]
        return run(*cmd)
    elif args.cmd == "all":
        rc = 0
        rc |= run("macro_overlay.py",
                  "--watchlist", *args.watchlist,
                  "--days", str(args.days_ahead))
        print("\n\n")
        rc |= run("options_screener.py",
                  "--watchlist", *args.watchlist,
                  "--strategies", *args.strategies,
                  "--min-dte", str(args.min_dte),
                  "--max-dte", str(args.max_dte),
                  "--target-delta", str(args.target_delta))
        print("\n\n")
        rc |= run("portfolio_risk.py",
                  "--target-watchlist", *args.watchlist)
        print("\n\n")
        rc |= run("iv_rank.py", "--tickers", *args.watchlist)
        print("\n\n")
        rc |= run("earnings_iv_scanner.py",
                  "--watchlist", *args.watchlist,
                  "--days-ahead", str(args.days_ahead))
        print("\n\n")
        rc |= run("earnings_backtest.py",
                  "--tickers", *args.watchlist,
                  "--num-events", str(args.num_events))
        return rc


if __name__ == "__main__":
    sys.exit(main())
