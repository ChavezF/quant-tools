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

    p_scenario = sub.add_parser("scenario-stress", help="Run deterministic portfolio shock scenarios")
    p_scenario.add_argument("--portfolio", required=True, help="Path to portfolio_risk --json output")
    p_scenario.add_argument("--scenarios", help="Optional JSON scenario definition file")
    p_scenario.add_argument("--output")
    p_scenario.add_argument("--json", action="store_true")

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
    p_plan.add_argument("--db")
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

    p_allocate = sub.add_parser("allocate", help="Select a portfolio-level basket from an action plan")
    p_allocate.add_argument("--plan", required=True)
    p_allocate.add_argument("--output")
    p_allocate.add_argument("--json", action="store_true")

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
    p_daily.add_argument("--sizing-mode", choices=["cautious", "normal", "aggressive"],
                         default="normal",
                         help="Scale per-NAV allocation caps: cautious=0.5x, normal=1.0x, aggressive=1.5x")
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
    p_daily.add_argument("--skip-scenario-stress", action="store_true")
    p_daily.add_argument("--skip-allocation", action="store_true")
    p_daily.add_argument("--skip-brief", action="store_true")
    p_daily.add_argument("--skip-alerts", action="store_true")
    p_daily.add_argument("--no-cache", action="store_true")
    p_daily.add_argument("--send", action="store_true")
    p_daily.add_argument("--dry-run", action="store_true")

    p_journal = sub.add_parser("journal", help="Trade journal add/list/close/stats")
    p_journal.add_argument("--state-file")
    p_journal.add_argument("--db")
    p_journal.add_argument("journal_args", nargs=argparse.REMAINDER)

    p_alerts = sub.add_parser("alerts", help="Generate alerts from plan and journal state")
    p_alerts.add_argument("--plan", help="Path to action_plan --json output")
    p_alerts.add_argument("--journal", help="Optional path to trade journal state")
    p_alerts.add_argument("--min-score", type=float, default=68.0)
    p_alerts.add_argument("--profit-target-pct", type=float, default=50.0)
    p_alerts.add_argument("--dte-warning", type=int, default=21)
    p_alerts.add_argument("--validation")
    p_alerts.add_argument("--drift")
    p_alerts.add_argument("--reconciliation")
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
    p_tickets.add_argument("--db")
    p_tickets.add_argument("--allow-duplicates", action="store_true")
    p_tickets.add_argument("--pending-expiry-hours", type=float)
    p_tickets.add_argument("--partial-review-hours", type=float)
    p_tickets.add_argument("--json", action="store_true")

    p_dashboard = sub.add_parser("dashboard", help="Generate static HTML dashboard from reports")
    p_dashboard.add_argument("--report-dir")
    p_dashboard.add_argument("--plan")
    p_dashboard.add_argument("--alerts")
    p_dashboard.add_argument("--tickets")
    p_dashboard.add_argument("--manifest")
    p_dashboard.add_argument("--analytics")
    p_dashboard.add_argument("--feedback")
    p_dashboard.add_argument("--reconciliation")
    p_dashboard.add_argument("--execution-analytics")
    p_dashboard.add_argument("--database-maintenance")
    p_dashboard.add_argument("--health")
    p_dashboard.add_argument("--scenario-stress")
    p_dashboard.add_argument("--allocation")
    p_dashboard.add_argument("--validation")
    p_dashboard.add_argument("--drift")
    p_dashboard.add_argument("--output")

    p_analytics = sub.add_parser("analytics", help="Analyze realized journal performance")
    p_analytics.add_argument("--journal")
    p_analytics.add_argument("--recent-window", type=int, default=10)
    p_analytics.add_argument("--output")
    p_analytics.add_argument("--json", action="store_true")

    p_feedback = sub.add_parser("feedback", help="Recommend score and sizing calibration")
    p_feedback.add_argument("--journal")
    p_feedback.add_argument("--db")
    p_feedback.add_argument("--current-min-score", type=float)
    p_feedback.add_argument("--min-samples", type=int)
    p_feedback.add_argument("--output")
    p_feedback.add_argument("--json", action="store_true")

    p_validate = sub.add_parser("validate", help="Walk-forward validate live score thresholds")
    p_validate.add_argument("--journal")
    p_validate.add_argument("--min-train", type=int)
    p_validate.add_argument("--test-window", type=int)
    p_validate.add_argument("--thresholds", nargs="+", type=float)
    p_validate.add_argument("--min-selected", type=int)
    p_validate.add_argument("--output")
    p_validate.add_argument("--json", action="store_true")

    p_drift = sub.add_parser("drift", help="Detect recent performance and calibration drift")
    p_drift.add_argument("--journal")
    p_drift.add_argument("--recent-window", type=int)
    p_drift.add_argument("--min-baseline", type=int)
    p_drift.add_argument("--current-min-score", type=float)
    p_drift.add_argument("--min-samples", type=int)
    p_drift.add_argument("--output")
    p_drift.add_argument("--json", action="store_true")

    p_operator = sub.add_parser("operator", help="Run the complete morning decision workflow")
    p_operator.add_argument("--report-dir")
    p_operator.add_argument("--journal")
    p_operator.add_argument("--broker-snapshot")
    p_operator.add_argument("--sizing-mode", choices=["cautious", "normal", "aggressive"],
                            default="normal",
                            help="Scale per-NAV allocation caps: cautious=0.5x, normal=1.0x, aggressive=1.5x")
    p_operator.add_argument("--dry-run", action="store_true")
    p_operator.add_argument("--send", action="store_true")
    p_operator.add_argument("--skip-brief", action="store_true")
    p_operator.add_argument("--skip-alerts", action="store_true")
    p_operator.add_argument("--skip-discovery", action="store_true")
    p_operator.add_argument("--skip-storage", action="store_true")
    p_operator.add_argument("--skip-scenario-stress", action="store_true")
    p_operator.add_argument("--skip-allocation", action="store_true")
    p_operator.add_argument("--no-cache", action="store_true")

    p_storage = sub.add_parser("storage", help="Sync workflow artifacts into SQLite")
    p_storage.add_argument("--db")
    p_storage.add_argument("--journal")
    p_storage.add_argument("--tickets")
    p_storage.add_argument("--portfolio")
    p_storage.add_argument("--broker-snapshot")
    p_storage.add_argument("--output")
    p_storage.add_argument("--export-journal")
    p_storage.add_argument("--pending-expiry-hours", type=float)
    p_storage.add_argument("--partial-review-hours", type=float)
    p_storage.add_argument("--json", action="store_true")

    p_ticket_lifecycle = sub.add_parser("ticket-lifecycle", help="Inspect or close persistent execution tickets")
    p_ticket_lifecycle.add_argument("--db")
    p_ticket_lifecycle.add_argument("--status", nargs="+")
    p_ticket_lifecycle.add_argument("--active", action="store_true")
    p_ticket_lifecycle.add_argument("--ticket-id")
    p_ticket_lifecycle.add_argument("--set-status", choices=["PENDING", "CANCELLED", "EXPIRED"])
    p_ticket_lifecycle.add_argument("--json", action="store_true")

    p_broker_sync = sub.add_parser("broker-sync", help="Fetch Public.com fills and positions into a broker snapshot")
    p_broker_sync.add_argument("--cursor")
    p_broker_sync.add_argument("--output")
    p_broker_sync.add_argument("--start")
    p_broker_sync.add_argument("--end")
    p_broker_sync.add_argument("--page-size", type=int)
    p_broker_sync.add_argument("--max-pages", type=int)
    p_broker_sync.add_argument("--overlap-minutes", type=int)
    p_broker_sync.add_argument("--full-refresh", action="store_true")
    p_broker_sync.add_argument("--json", action="store_true")

    p_reconcile = sub.add_parser("reconcile", help="Reconcile tickets and journal against broker data")
    p_reconcile.add_argument("--journal", required=True)
    p_reconcile.add_argument("--tickets", required=True)
    p_reconcile.add_argument("--broker-snapshot", required=True)
    p_reconcile.add_argument("--output")
    p_reconcile.add_argument("--apply-updates", action="store_true")
    p_reconcile.add_argument("--journal-output")
    p_reconcile.add_argument("--db")
    p_reconcile.add_argument("--json", action="store_true")

    p_execution = sub.add_parser("execution-analytics", help="Measure fill quality and ticket execution")
    p_execution.add_argument("--tickets", required=True)
    p_execution.add_argument("--reconciliation", required=True)
    p_execution.add_argument("--output")
    p_execution.add_argument("--json", action="store_true")

    p_execution_history = sub.add_parser(
        "execution-history",
        help="Build durable execution attribution from reconciliation history",
    )
    p_execution_history.add_argument("--db")
    p_execution_history.add_argument("--min-samples", type=int, default=5)
    p_execution_history.add_argument("--output")
    p_execution_history.add_argument("--json", action="store_true")

    p_verify = sub.add_parser("verify", help="Run repository and runtime health checks")
    p_verify.add_argument("--db")
    p_verify.add_argument("--skip-tests", action="store_true")
    p_verify.add_argument("--skip-db", action="store_true")
    p_verify.add_argument("--output")
    p_verify.add_argument("--json", action="store_true")

    p_db_maintenance = sub.add_parser("db-maintenance", help="Check, back up, and retain SQLite state")
    p_db_maintenance.add_argument("--db")
    p_db_maintenance.add_argument("--backup-dir")
    p_db_maintenance.add_argument("--retention-days", type=int)
    p_db_maintenance.add_argument("--keep-last", type=int)
    p_db_maintenance.add_argument("--no-backup", action="store_true")
    p_db_maintenance.add_argument("--vacuum", action="store_true")
    p_db_maintenance.add_argument("--output")
    p_db_maintenance.add_argument("--json", action="store_true")

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

    p_mc = sub.add_parser("monte-carlo", help="Monte Carlo stress test for 16-delta strangle")
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

    def pick(value, default):
        return value if value is not None else default

    def journal_path(override: str | None) -> str | None:
        return resolve_project_path(override or cfg.get("journal", {}).get("path"))

    def db_path_arg(override: str | None) -> str | None:
        return resolve_project_path(override or cfg.get("storage", {}).get("path"))

    def extend_opt(cmd: list[str], flag: str, value: str | None) -> None:
        if value:
            cmd += [flag, value]

    if args.cmd == "scan":
        watchlist = args.watchlist or cfg["watchlists"].get(args.watchlist_name)
        if not watchlist:
            raise SystemExit(f"Unknown watchlist: {args.watchlist_name}")
        strategies = args.strategies or scan_cfg["strategies"]
        min_dte = pick(args.min_dte, scan_cfg["min_dte"])
        max_dte = pick(args.max_dte, scan_cfg["max_dte"])
        target_delta = pick(args.target_delta, scan_cfg["target_delta"])
        min_oi = pick(args.min_oi, scan_cfg["min_oi"])
        max_expirations = pick(args.max_expirations, scan_cfg.get("max_expirations", 1))
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
        extend_opt(cmd, "--journal", journal_path(args.journal))
        extend_opt(cmd, "--db", db_path_arg(args.db))
        if args.json:
            cmd += ["--json"]
        return run(*cmd)
    elif args.cmd == "allocate":
        cmd = ["portfolio_allocator.py", "--plan", args.plan]
        if args.config:
            cmd += ["--config", args.config]
        if args.output:
            cmd += ["--output", args.output]
        if args.json:
            cmd += ["--json"]
        return run(*cmd)
    elif args.cmd == "scenario-stress":
        cmd = ["scenario_stress.py", "--portfolio", args.portfolio]
        if args.scenarios:
            cmd += ["--scenarios", args.scenarios]
        if args.output:
            cmd += ["--output", args.output]
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
            ("sizing_mode", "--sizing-mode"),
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
            ("skip_scenario_stress", "--skip-scenario-stress"),
            ("skip_allocation", "--skip-allocation"),
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
        cmd = ["trade_journal.py"]
        if args.state_file:
            cmd += ["--state-file", args.state_file]
        if args.db:
            cmd += ["--db", args.db]
        cmd += args.journal_args
        return run(*cmd)
    elif args.cmd == "alerts":
        cmd = [
            "alerts.py",
            "--min-score", str(args.min_score),
            "--profit-target-pct", str(args.profit_target_pct),
            "--dte-warning", str(args.dte_warning),
        ]
        if args.plan:
            cmd += ["--plan", args.plan]
        if args.validation:
            cmd += ["--validation", args.validation]
        if args.drift:
            cmd += ["--drift", args.drift]
        if args.reconciliation:
            cmd += ["--reconciliation", args.reconciliation]
        extend_opt(cmd, "--journal", journal_path(args.journal))
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
        extend_opt(cmd, "--db", db_path_arg(args.db))
        lifecycle_cfg = cfg.get("execution_lifecycle", {})
        cmd += [
            "--pending-expiry-hours",
            str(pick(args.pending_expiry_hours, lifecycle_cfg.get("pending_expiry_hours", 24))),
            "--partial-review-hours",
            str(pick(args.partial_review_hours, lifecycle_cfg.get("partial_review_hours", 4))),
        ]
        if args.allow_duplicates:
            cmd += ["--allow-duplicates"]
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
            ("analytics", "--analytics"),
            ("feedback", "--feedback"),
            ("reconciliation", "--reconciliation"),
            ("execution_analytics", "--execution-analytics"),
            ("database_maintenance", "--database-maintenance"),
            ("health", "--health"),
            ("scenario_stress", "--scenario-stress"),
            ("allocation", "--allocation"),
            ("validation", "--validation"),
            ("drift", "--drift"),
            ("output", "--output"),
        ]:
            value = getattr(args, attr)
            if value:
                cmd += [flag, value]
        return run(*cmd)
    elif args.cmd == "analytics":
        cmd = ["historical_analytics.py"]
        extend_opt(cmd, "--journal", journal_path(args.journal))
        cmd += ["--recent-window", str(args.recent_window)]
        if args.output:
            cmd += ["--output", args.output]
        if args.json:
            cmd += ["--json"]
        return run(*cmd)
    elif args.cmd == "feedback":
        cmd = ["feedback_calibration.py"]
        extend_opt(cmd, "--journal", journal_path(args.journal))
        extend_opt(cmd, "--db", db_path_arg(args.db))
        cmd += [
            "--current-min-score", str(pick(args.current_min_score, risk_cfg["min_score"])),
            "--min-samples", str(pick(args.min_samples, cfg.get("feedback", {}).get("min_samples", 5))),
        ]
        if args.output:
            cmd += ["--output", args.output]
        if args.json:
            cmd += ["--json"]
        return run(*cmd)
    elif args.cmd == "validate":
        validation_cfg = cfg.get("validation", {})
        cmd = ["walk_forward_validation.py"]
        extend_opt(cmd, "--journal", journal_path(args.journal))
        cmd += [
            "--min-train", str(pick(args.min_train, validation_cfg.get("min_train", 10))),
            "--test-window", str(pick(args.test_window, validation_cfg.get("test_window", 5))),
            "--min-selected", str(pick(args.min_selected, validation_cfg.get("min_selected", 3))),
        ]
        thresholds = args.thresholds or validation_cfg.get("thresholds", [50, 55, 60, 65, 70, 75])
        cmd += ["--thresholds", *[str(value) for value in thresholds]]
        if args.output:
            cmd += ["--output", args.output]
        if args.json:
            cmd += ["--json"]
        return run(*cmd)
    elif args.cmd == "drift":
        drift_cfg = cfg.get("drift_monitor", {})
        cmd = ["drift_monitor.py"]
        extend_opt(cmd, "--journal", journal_path(args.journal))
        cmd += [
            "--recent-window", str(pick(args.recent_window, drift_cfg.get("recent_window", 10))),
            "--min-baseline", str(pick(args.min_baseline, drift_cfg.get("min_baseline", 10))),
            "--current-min-score", str(pick(args.current_min_score, risk_cfg["min_score"])),
            "--min-samples", str(pick(args.min_samples, cfg.get("feedback", {}).get("min_samples", 5))),
        ]
        if args.output:
            cmd += ["--output", args.output]
        if args.json:
            cmd += ["--json"]
        return run(*cmd)
    elif args.cmd == "operator":
        cmd = ["daily_workflow.py"]
        if args.config:
            cmd += ["--config", args.config]
        if args.report_dir:
            cmd += ["--report-dir", args.report_dir]
        if args.journal:
            cmd += ["--journal", args.journal]
        if args.broker_snapshot:
            cmd += ["--broker-snapshot", args.broker_snapshot]
        if getattr(args, "sizing_mode", None):
            cmd += ["--sizing-mode", args.sizing_mode]
        for flag_name, flag in [
            ("dry_run", "--dry-run"),
            ("send", "--send"),
            ("skip_brief", "--skip-brief"),
            ("skip_alerts", "--skip-alerts"),
            ("skip_discovery", "--skip-discovery"),
            ("skip_storage", "--skip-storage"),
            ("skip_scenario_stress", "--skip-scenario-stress"),
            ("skip_allocation", "--skip-allocation"),
            ("no_cache", "--no-cache"),
        ]:
            if getattr(args, flag_name):
                cmd += [flag]
        return run(*cmd)
    elif args.cmd == "storage":
        cmd = ["storage_sync.py"]
        lifecycle_cfg = cfg.get("execution_lifecycle", {})
        extend_opt(cmd, "--db", db_path_arg(args.db))
        extend_opt(cmd, "--journal", journal_path(args.journal))
        cmd += [
            "--pending-expiry-hours",
            str(pick(args.pending_expiry_hours, lifecycle_cfg.get("pending_expiry_hours", 24))),
            "--partial-review-hours",
            str(pick(args.partial_review_hours, lifecycle_cfg.get("partial_review_hours", 4))),
        ]
        for attr, flag in [
            ("tickets", "--tickets"),
            ("portfolio", "--portfolio"),
            ("broker_snapshot", "--broker-snapshot"),
            ("output", "--output"),
            ("export_journal", "--export-journal"),
        ]:
            value = getattr(args, attr)
            if value:
                cmd += [flag, value]
        if args.json:
            cmd += ["--json"]
        return run(*cmd)
    elif args.cmd == "ticket-lifecycle":
        cmd = ["ticket_lifecycle.py"]
        extend_opt(cmd, "--db", db_path_arg(args.db))
        if args.status:
            cmd += ["--status", *args.status]
        if args.active:
            cmd += ["--active"]
        if args.ticket_id:
            cmd += ["--ticket-id", args.ticket_id]
        if args.set_status:
            cmd += ["--set-status", args.set_status]
        if args.json:
            cmd += ["--json"]
        return run(*cmd)
    elif args.cmd == "broker-sync":
        ingestion_cfg = cfg.get("public_ingestion", {})
        cmd = [
            "public_fill_ingestion.py",
            "--cursor",
            resolve_project_path(args.cursor or ingestion_cfg.get("cursor_path"))
            or str(SCRIPT_DIR.parent / "state" / "public_fill_cursor.json"),
            "--output",
            resolve_project_path(args.output or ingestion_cfg.get("snapshot_path"))
            or str(SCRIPT_DIR.parent / "state" / "public_broker_snapshot.json"),
            "--page-size", str(pick(args.page_size, ingestion_cfg.get("page_size", 100))),
            "--max-pages", str(pick(args.max_pages, ingestion_cfg.get("max_pages", 100))),
            "--overlap-minutes", str(pick(args.overlap_minutes, ingestion_cfg.get("overlap_minutes", 15))),
        ]
        if args.start:
            cmd += ["--start", args.start]
        if args.end:
            cmd += ["--end", args.end]
        if args.full_refresh:
            cmd += ["--full-refresh"]
        if args.json:
            cmd += ["--json"]
        return run(*cmd)
    elif args.cmd == "reconcile":
        cmd = [
            "broker_reconciliation.py",
            "--journal",
            args.journal,
            "--tickets",
            args.tickets,
            "--broker-snapshot",
            args.broker_snapshot,
        ]
        if args.output:
            cmd += ["--output", args.output]
        if args.apply_updates:
            cmd += ["--apply-updates"]
        if args.journal_output:
            cmd += ["--journal-output", args.journal_output]
        if args.db:
            cmd += ["--db", args.db]
        if args.json:
            cmd += ["--json"]
        return run(*cmd)
    elif args.cmd == "execution-analytics":
        cmd = [
            "execution_analytics.py",
            "--tickets",
            args.tickets,
            "--reconciliation",
            args.reconciliation,
        ]
        if args.output:
            cmd += ["--output", args.output]
        if args.json:
            cmd += ["--json"]
        return run(*cmd)
    elif args.cmd == "execution-history":
        cmd = [
            "execution_attribution.py",
            "--min-samples",
            str(args.min_samples),
        ]
        extend_opt(cmd, "--db", db_path_arg(args.db))
        if args.output:
            cmd += ["--output", args.output]
        if args.json:
            cmd += ["--json"]
        return run(*cmd)
    elif args.cmd == "verify":
        cmd = ["health_check.py"]
        extend_opt(cmd, "--db", db_path_arg(args.db))
        if args.skip_tests:
            cmd += ["--skip-tests"]
        if args.skip_db:
            cmd += ["--skip-db"]
        if args.output:
            cmd += ["--output", args.output]
        if args.json:
            cmd += ["--json"]
        return run(*cmd)
    elif args.cmd == "db-maintenance":
        operations = cfg.get("operations", {})
        cmd = ["database_maintenance.py"]
        extend_opt(cmd, "--db", db_path_arg(args.db))
        extend_opt(cmd, "--backup-dir", resolve_project_path(args.backup_dir or operations.get("backup_dir")))
        cmd += [
            "--retention-days", str(pick(args.retention_days, operations.get("backup_retention_days", 30))),
            "--keep-last", str(pick(args.keep_last, operations.get("backup_keep_last", 14))),
        ]
        if args.no_backup:
            cmd += ["--no-backup"]
        if args.vacuum:
            cmd += ["--vacuum"]
        if args.output:
            cmd += ["--output", args.output]
        if args.json:
            cmd += ["--json"]
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
