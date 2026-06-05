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
from toolkit_config import add_config_argument, load_config, resolve_project_path

SCRIPT_DIR = Path(__file__).parent
PY = os.environ.get("QUANT_PYTHON", sys.executable)


def run(script: str, *args: str) -> int:
    cmd = [PY, str(SCRIPT_DIR / script), *args]
    return subprocess.call(cmd)


def main():
    ap = argparse.ArgumentParser()
    add_config_argument(ap)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan", help="Options screener")
    p_scan.add_argument("--watchlist", nargs="+")
    p_scan.add_argument("--watchlist-name", default="core")
    p_scan.add_argument("--strategies", nargs="+")
    p_scan.add_argument("--min-dte", type=int)
    p_scan.add_argument("--max-dte", type=int)
    p_scan.add_argument("--target-delta", type=float)
    p_scan.add_argument("--min-oi", type=int)
    p_scan.add_argument("--max-expirations", type=int)
    p_scan.add_argument("--wing-widths", nargs="+", type=float)
    p_scan.add_argument("--no-cache", action="store_true")
    p_scan.add_argument("--ranked", action="store_true")
    p_scan.add_argument("--json", action="store_true", help="Emit screener output as JSON (pipe into pretrade/plan)")
    p_scan.add_argument("--report", help="Path to write the screener JSON report")

    p_risk = sub.add_parser("risk", help="Portfolio risk")
    p_risk.add_argument("--target-watchlist", nargs="+")

    p_pre = sub.add_parser("pretrade", help="Risk-check scored candidates before execution")
    p_pre.add_argument("--candidates", required=True, help="Path to options_screener JSON report")
    p_pre.add_argument("--portfolio", help="Optional path to portfolio_risk --json output")
    p_pre.add_argument("--account-nav", type=float)
    p_pre.add_argument("--max-trade-risk-pct", type=float)
    p_pre.add_argument("--max-trade-bp-pct", type=float)
    p_pre.add_argument("--max-single-ticker-pct", type=float)
    p_pre.add_argument("--max-portfolio-delta-abs", type=float)
    p_pre.add_argument("--min-score", type=float)
    p_pre.add_argument("--min-liquidity-score", type=float)
    p_pre.add_argument("--min-pop-pct", type=float)
    p_pre.add_argument("--json", action="store_true")

    p_plan = sub.add_parser("plan", help="Build ranked action plan from scan/risk/journal reports")
    p_plan.add_argument("--candidates", required=True, help="Path to options_screener JSON report")
    p_plan.add_argument("--portfolio", help="Optional path to portfolio_risk --json output")
    p_plan.add_argument("--journal", help="Optional path to trade journal state")
    p_plan.add_argument("--account-nav", type=float)
    p_plan.add_argument("--max-trade-risk-pct", type=float)
    p_plan.add_argument("--max-trade-bp-pct", type=float)
    p_plan.add_argument("--max-single-ticker-pct", type=float)
    p_plan.add_argument("--max-portfolio-delta-abs", type=float)
    p_plan.add_argument("--min-score", type=float)
    p_plan.add_argument("--min-liquidity-score", type=float)
    p_plan.add_argument("--min-pop-pct", type=float)
    p_plan.add_argument("--top", type=int, default=10)
    p_plan.add_argument("--json", action="store_true")

    p_daily = sub.add_parser("daily", help="Run saved daily workflow into timestamped reports")
    p_daily.add_argument("--watchlist", nargs="+")
    p_daily.add_argument("--watchlist-name", default="core")
    p_daily.add_argument("--strategies", nargs="+")
    p_daily.add_argument("--min-dte", type=int)
    p_daily.add_argument("--max-dte", type=int)
    p_daily.add_argument("--target-delta", type=float)
    p_daily.add_argument("--min-oi", type=int)
    p_daily.add_argument("--max-expirations", type=int)
    p_daily.add_argument("--wing-widths", nargs="+", type=float)
    p_daily.add_argument("--target-watchlist", nargs="+")
    p_daily.add_argument("--journal")
    p_daily.add_argument("--report-dir")
    p_daily.add_argument("--account-nav", type=float)
    p_daily.add_argument("--max-trade-risk-pct", type=float)
    p_daily.add_argument("--max-trade-bp-pct", type=float)
    p_daily.add_argument("--max-single-ticker-pct", type=float)
    p_daily.add_argument("--max-portfolio-delta-abs", type=float)
    p_daily.add_argument("--min-score", type=float)
    p_daily.add_argument("--min-liquidity-score", type=float)
    p_daily.add_argument("--min-pop-pct", type=float)
    p_daily.add_argument("--alert-min-score", type=float)
    p_daily.add_argument("--profit-target-pct", type=float)
    p_daily.add_argument("--dte-warning", type=int)
    p_daily.add_argument("--top", type=int, default=10)
    p_daily.add_argument("--skip-discovery", action="store_true")
    p_daily.add_argument("--skip-risk", action="store_true")
    p_daily.add_argument("--skip-brief", action="store_true")
    p_daily.add_argument("--skip-alerts", action="store_true")
    p_daily.add_argument("--no-cache", action="store_true")
    p_daily.add_argument("--send", action="store_true")
    p_daily.add_argument("--dry-run", action="store_true")

    p_journal = sub.add_parser("journal", help="Trade journal add/list/close/stats")
    p_journal.add_argument("journal_args", nargs=argparse.REMAINDER)

    p_alerts = sub.add_parser("alerts", help="Generate alerts from plan and journal state")
    p_alerts.add_argument("--plan", help="Path to action_plan --json output")
    p_alerts.add_argument("--journal", help="Optional path to trade journal state")
    p_alerts.add_argument("--min-score", type=float, default=68.0)
    p_alerts.add_argument("--profit-target-pct", type=float, default=50.0)
    p_alerts.add_argument("--dte-warning", type=int, default=21)
    p_alerts.add_argument("--json", action="store_true")

    p_discover = sub.add_parser("discover", help="Discover symbols worth scanning")
    p_discover.add_argument("--symbols", nargs="+")
    p_discover.add_argument("--watchlist-name", default="discovery")
    p_discover.add_argument("--min-price", type=float)
    p_discover.add_argument("--min-avg-volume", type=float)
    p_discover.add_argument("--top", type=int)
    p_discover.add_argument("--json", action="store_true")

    p_tickets = sub.add_parser("tickets", help="Build execution tickets from an action plan")
    p_tickets.add_argument("--plan", required=True)
    p_tickets.add_argument("--approve-only", action="store_true")
    p_tickets.add_argument("--json", action="store_true")

    p_dashboard = sub.add_parser("dashboard", help="Generate static HTML dashboard from reports")
    p_dashboard.add_argument("--report-dir")
    p_dashboard.add_argument("--plan")
    p_dashboard.add_argument("--alerts")
    p_dashboard.add_argument("--tickets")
    p_dashboard.add_argument("--manifest")
    p_dashboard.add_argument("--output")

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
    cfg = load_config(args.config)
    scan_cfg = cfg["scan"]
    risk_cfg = cfg["risk_limits"]

    def risk_value(name: str):
        return getattr(args, name) if getattr(args, name) is not None else risk_cfg[name]

    if args.cmd == "scan":
        watchlist = args.watchlist or cfg["watchlists"].get(args.watchlist_name)
        if not watchlist:
            raise SystemExit(f"Unknown watchlist: {args.watchlist_name}")
        strategies = args.strategies or scan_cfg["strategies"]
        min_dte = args.min_dte if args.min_dte is not None else scan_cfg["min_dte"]
        max_dte = args.max_dte if args.max_dte is not None else scan_cfg["max_dte"]
        target_delta = args.target_delta if args.target_delta is not None else scan_cfg["target_delta"]
        min_oi = args.min_oi if args.min_oi is not None else scan_cfg["min_oi"]
        max_expirations = args.max_expirations if args.max_expirations is not None else scan_cfg.get("max_expirations", 1)
        wing_widths = args.wing_widths or scan_cfg.get("wing_widths", [5.0])
        return run("options_screener.py",
                   *(["--config", args.config] if args.config else []),
                   "--watchlist", *watchlist,
                   "--strategies", *strategies,
                   "--min-dte", str(min_dte),
                   "--max-dte", str(max_dte),
                   "--target-delta", str(target_delta),
                   "--min-oi", str(min_oi),
                   "--max-expirations", str(max_expirations),
                   "--wing-widths", *[str(w) for w in wing_widths],
                   *(["--no-cache"] if args.no_cache else []),
                   *(["--ranked"] if args.ranked else []),
                   *(["--json"] if args.json else []),
                   *(["--report", args.report] if args.report else []))
    elif args.cmd == "risk":
        cmd = ["portfolio_risk.py"]
        if args.target_watchlist:
            cmd += ["--target-watchlist", *args.target_watchlist]
        return run(*cmd)
    elif args.cmd == "pretrade":
        cmd = [
            "pretrade_check.py",
            "--candidates", args.candidates,
            "--account-nav", str(risk_value("account_nav")),
            "--max-trade-risk-pct", str(risk_value("max_trade_risk_pct")),
            "--max-trade-bp-pct", str(risk_value("max_trade_bp_pct")),
            "--max-single-ticker-pct", str(risk_value("max_single_ticker_pct")),
            "--max-portfolio-delta-abs", str(risk_value("max_portfolio_delta_abs")),
            "--min-score", str(risk_value("min_score")),
            "--min-liquidity-score", str(risk_value("min_liquidity_score")),
            "--min-pop-pct", str(risk_value("min_pop_pct")),
        ]
        if args.portfolio:
            cmd += ["--portfolio", args.portfolio]
        if args.json:
            cmd += ["--json"]
        return run(*cmd)
    elif args.cmd == "plan":
        cmd = [
            "action_plan.py",
            *(["--config", args.config] if args.config else []),
            "--candidates", args.candidates,
            "--account-nav", str(risk_value("account_nav")),
            "--max-trade-risk-pct", str(risk_value("max_trade_risk_pct")),
            "--max-trade-bp-pct", str(risk_value("max_trade_bp_pct")),
            "--max-single-ticker-pct", str(risk_value("max_single_ticker_pct")),
            "--max-portfolio-delta-abs", str(risk_value("max_portfolio_delta_abs")),
            "--min-score", str(risk_value("min_score")),
            "--min-liquidity-score", str(risk_value("min_liquidity_score")),
            "--min-pop-pct", str(risk_value("min_pop_pct")),
            "--top", str(args.top),
        ]
        if args.portfolio:
            cmd += ["--portfolio", args.portfolio]
        journal = args.journal or cfg.get("journal", {}).get("path")
        if journal:
            cmd += ["--journal", resolve_project_path(journal)]
        if args.json:
            cmd += ["--json"]
        return run(*cmd)
    elif args.cmd == "daily":
        cmd = ["daily_workflow.py"]
        if args.config:
            cmd += ["--config", args.config]
        if args.watchlist:
            cmd += ["--watchlist", *args.watchlist]
        if args.watchlist_name:
            cmd += ["--watchlist-name", args.watchlist_name]
        if args.strategies:
            cmd += ["--strategies", *args.strategies]
        for attr, flag in [
            ("min_dte", "--min-dte"),
            ("max_dte", "--max-dte"),
            ("target_delta", "--target-delta"),
            ("min_oi", "--min-oi"),
            ("max_expirations", "--max-expirations"),
            ("account_nav", "--account-nav"),
            ("max_trade_risk_pct", "--max-trade-risk-pct"),
            ("max_trade_bp_pct", "--max-trade-bp-pct"),
            ("max_single_ticker_pct", "--max-single-ticker-pct"),
            ("max_portfolio_delta_abs", "--max-portfolio-delta-abs"),
            ("min_score", "--min-score"),
            ("min_liquidity_score", "--min-liquidity-score"),
            ("min_pop_pct", "--min-pop-pct"),
            ("alert_min_score", "--alert-min-score"),
            ("profit_target_pct", "--profit-target-pct"),
            ("dte_warning", "--dte-warning"),
            ("top", "--top"),
        ]:
            value = getattr(args, attr)
            if value is not None:
                cmd += [flag, str(value)]
        if args.wing_widths:
            cmd += ["--wing-widths", *[str(w) for w in args.wing_widths]]
        if args.target_watchlist:
            cmd += ["--target-watchlist", *args.target_watchlist]
        if args.journal:
            cmd += ["--journal", args.journal]
        if args.report_dir:
            cmd += ["--report-dir", args.report_dir]
        for flag_name, flag in [
            ("skip_discovery", "--skip-discovery"),
            ("skip_risk", "--skip-risk"),
            ("skip_brief", "--skip-brief"),
            ("skip_alerts", "--skip-alerts"),
            ("no_cache", "--no-cache"),
            ("send", "--send"),
            ("dry_run", "--dry-run"),
        ]:
            if getattr(args, flag_name):
                cmd += [flag]
        return run(*cmd)
    elif args.cmd == "journal":
        return run("trade_journal.py", *args.journal_args)
    elif args.cmd == "alerts":
        cmd = [
            "alerts.py",
            "--min-score", str(args.min_score),
            "--profit-target-pct", str(args.profit_target_pct),
            "--dte-warning", str(args.dte_warning),
        ]
        if args.plan:
            cmd += ["--plan", args.plan]
        journal = args.journal or cfg.get("journal", {}).get("path")
        if journal:
            cmd += ["--journal", resolve_project_path(journal)]
        if args.json:
            cmd += ["--json"]
        return run(*cmd)
    elif args.cmd == "discover":
        cmd = ["opportunity_discovery.py"]
        if args.config:
            cmd += ["--config", args.config]
        if args.symbols:
            cmd += ["--symbols", *args.symbols]
        if args.watchlist_name:
            cmd += ["--watchlist-name", args.watchlist_name]
        for attr, flag in [
            ("min_price", "--min-price"),
            ("min_avg_volume", "--min-avg-volume"),
            ("top", "--top"),
        ]:
            value = getattr(args, attr)
            if value is not None:
                cmd += [flag, str(value)]
        if args.json:
            cmd += ["--json"]
        return run(*cmd)
    elif args.cmd == "tickets":
        cmd = ["execution_tickets.py", "--plan", args.plan]
        if args.approve_only:
            cmd += ["--approve-only"]
        if args.json:
            cmd += ["--json"]
        return run(*cmd)
    elif args.cmd == "dashboard":
        cmd = ["dashboard.py"]
        for attr, flag in [
            ("report_dir", "--report-dir"),
            ("plan", "--plan"),
            ("alerts", "--alerts"),
            ("tickets", "--tickets"),
            ("manifest", "--manifest"),
            ("output", "--output"),
        ]:
            value = getattr(args, attr)
            if value:
                cmd += [flag, value]
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
