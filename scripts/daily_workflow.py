#!/usr/bin/env python3.12
"""
daily_workflow.py - run the saved daily quant workflow.

Pipeline:
  analytics -> feedback -> discovery -> scan -> risk -> plan -> alerts ->
  tickets -> brief -> operator summary -> dashboard

Each run writes into reports/YYYYMMDD-HHMMSS/ so the morning process is
repeatable and auditable.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from common import PROJECT_ROOT, SCRIPTS_DIR
from toolkit_config import add_config_argument, load_config, resolve_project_path


PY = os.environ.get("QUANT_PYTHON", sys.executable)


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def ensure_report_dir(raw_dir: str | None = None) -> Path:
    base = Path(raw_dir) if raw_dir else PROJECT_ROOT / "reports"
    if not base.is_absolute():
        base = PROJECT_ROOT / base
    run_dir = base / timestamp()
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def run_command(name: str, cmd: list[str], run_dir: Path, dry_run: bool = False) -> dict[str, Any]:
    stdout_path = run_dir / f"{name}.out"
    stderr_path = run_dir / f"{name}.err"
    meta = {
        "name": name,
        "cmd": cmd,
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
        "returncode": None,
    }
    if dry_run:
        meta["dry_run"] = True
        return meta

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    stdout_path.write_text(proc.stdout)
    stderr_path.write_text(proc.stderr)
    meta["returncode"] = proc.returncode
    return meta


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, default=str))


def build_scan_cmd(cfg: dict[str, Any], args: argparse.Namespace, scan_report: Path) -> list[str]:
    scan_cfg = cfg["scan"]
    watchlist = args.watchlist or cfg["watchlists"].get(args.watchlist_name)
    if not watchlist:
        raise SystemExit(f"Unknown watchlist: {args.watchlist_name}")
    strategies = args.strategies or scan_cfg["strategies"]
    return [
        PY, str(SCRIPTS_DIR / "options_screener.py"),
        *(["--config", args.config] if args.config else []),
        "--watchlist", *watchlist,
        "--strategies", *strategies,
        "--min-dte", str(args.min_dte if args.min_dte is not None else scan_cfg["min_dte"]),
        "--max-dte", str(args.max_dte if args.max_dte is not None else scan_cfg["max_dte"]),
        "--target-delta", str(args.target_delta if args.target_delta is not None else scan_cfg["target_delta"]),
        "--min-oi", str(args.min_oi if args.min_oi is not None else scan_cfg["min_oi"]),
        "--max-expirations", str(args.max_expirations if args.max_expirations is not None else scan_cfg.get("max_expirations", 1)),
        "--wing-widths", *[str(w) for w in (args.wing_widths or scan_cfg.get("wing_widths", [5.0]))],
        "--ranked",
        "--json",
        "--report", str(scan_report),
        *(["--no-cache"] if args.no_cache else []),
    ]


def build_discovery_cmd(cfg: dict[str, Any], args: argparse.Namespace) -> list[str]:
    disc_cfg = cfg.get("discovery", {})
    return [
        PY, str(SCRIPTS_DIR / "opportunity_discovery.py"),
        *(["--config", args.config] if args.config else []),
        "--watchlist-name", str(disc_cfg.get("watchlist_name", "discovery")),
        "--top", str(disc_cfg.get("top", 20)),
        "--json",
    ]


def journal_path(cfg: dict[str, Any], args: argparse.Namespace) -> str:
    return resolve_project_path(args.journal or cfg.get("journal", {}).get("path")) or ""


def build_analytics_cmd(cfg: dict[str, Any], args: argparse.Namespace) -> list[str]:
    return [
        PY,
        str(SCRIPTS_DIR / "historical_analytics.py"),
        "--journal",
        journal_path(cfg, args),
        "--json",
    ]


def build_feedback_cmd(cfg: dict[str, Any], args: argparse.Namespace) -> list[str]:
    feedback_cfg = cfg.get("feedback", {})
    return [
        PY,
        str(SCRIPTS_DIR / "feedback_calibration.py"),
        "--journal",
        journal_path(cfg, args),
        "--current-min-score",
        str(args.min_score if args.min_score is not None else cfg["risk_limits"]["min_score"]),
        "--min-samples",
        str(feedback_cfg.get("min_samples", 5)),
        "--json",
    ]


def build_risk_cmd(args: argparse.Namespace, watchlist: list[str] | None) -> list[str]:
    cmd = [PY, str(SCRIPTS_DIR / "portfolio_risk.py"), "--json"]
    if args.target_watchlist:
        cmd += ["--target-watchlist", *args.target_watchlist]
    elif watchlist:
        cmd += ["--target-watchlist", *watchlist]
    return cmd


def build_plan_cmd(cfg: dict[str, Any], args: argparse.Namespace, scan_report: Path, risk_report: Path | None) -> list[str]:
    risk_cfg = cfg["risk_limits"]
    journal = args.journal or cfg.get("journal", {}).get("path")
    cmd = [
        PY, str(SCRIPTS_DIR / "action_plan.py"),
        *(["--config", args.config] if args.config else []),
        "--candidates", str(scan_report),
        "--account-nav", str(args.account_nav if args.account_nav is not None else risk_cfg["account_nav"]),
        "--max-trade-risk-pct", str(args.max_trade_risk_pct if args.max_trade_risk_pct is not None else risk_cfg["max_trade_risk_pct"]),
        "--max-trade-bp-pct", str(args.max_trade_bp_pct if args.max_trade_bp_pct is not None else risk_cfg["max_trade_bp_pct"]),
        "--max-single-ticker-pct", str(args.max_single_ticker_pct if args.max_single_ticker_pct is not None else risk_cfg["max_single_ticker_pct"]),
        "--max-portfolio-delta-abs", str(args.max_portfolio_delta_abs if args.max_portfolio_delta_abs is not None else risk_cfg["max_portfolio_delta_abs"]),
        "--min-score", str(args.min_score if args.min_score is not None else risk_cfg["min_score"]),
        "--min-liquidity-score", str(args.min_liquidity_score if args.min_liquidity_score is not None else risk_cfg["min_liquidity_score"]),
        "--min-pop-pct", str(args.min_pop_pct if args.min_pop_pct is not None else risk_cfg["min_pop_pct"]),
        "--top", str(args.top),
        "--json",
    ]
    if risk_report:
        cmd += ["--portfolio", str(risk_report)]
    if journal:
        cmd += ["--journal", resolve_project_path(journal)]
    return cmd


def build_brief_cmd(args: argparse.Namespace, watchlist: list[str] | None) -> list[str]:
    cmd = [PY, str(SCRIPTS_DIR / "daily_brief.py"), "--dry-run"]
    if watchlist:
        cmd += ["--watchlist", *watchlist]
    if args.send:
        cmd = [PY, str(SCRIPTS_DIR / "daily_brief.py"), "--send"]
        if watchlist:
            cmd += ["--watchlist", *watchlist]
    return cmd


def build_alerts_cmd(cfg: dict[str, Any], args: argparse.Namespace, plan_report: Path) -> list[str]:
    alert_cfg = cfg.get("alerts", {})
    journal = args.journal or cfg.get("journal", {}).get("path")
    cmd = [
        PY, str(SCRIPTS_DIR / "alerts.py"),
        "--plan", str(plan_report),
        "--min-score", str(args.alert_min_score if args.alert_min_score is not None else alert_cfg.get("min_score", 68.0)),
        "--profit-target-pct", str(args.profit_target_pct if args.profit_target_pct is not None else alert_cfg.get("profit_target_pct", 50.0)),
        "--dte-warning", str(args.dte_warning if args.dte_warning is not None else alert_cfg.get("dte_warning", 21)),
        "--json",
    ]
    if journal:
        cmd += ["--journal", resolve_project_path(journal)]
    return cmd


def build_tickets_cmd(plan_report: Path) -> list[str]:
    return [PY, str(SCRIPTS_DIR / "execution_tickets.py"), "--plan", str(plan_report), "--json"]


def build_dashboard_cmd(run_dir: Path) -> list[str]:
    return [PY, str(SCRIPTS_DIR / "dashboard.py"), "--report-dir", str(run_dir)]


def build_operator_summary_cmd(run_dir: Path) -> list[str]:
    return [PY, str(SCRIPTS_DIR / "operator_summary.py"), "--report-dir", str(run_dir)]


def main() -> None:
    ap = argparse.ArgumentParser()
    add_config_argument(ap)
    ap.add_argument("--watchlist", nargs="+")
    ap.add_argument("--watchlist-name", default="core")
    ap.add_argument("--strategies", nargs="+")
    ap.add_argument("--min-dte", type=int)
    ap.add_argument("--max-dte", type=int)
    ap.add_argument("--target-delta", type=float)
    ap.add_argument("--min-oi", type=int)
    ap.add_argument("--max-expirations", type=int)
    ap.add_argument("--wing-widths", nargs="+", type=float)
    ap.add_argument("--target-watchlist", nargs="+")
    ap.add_argument("--journal")
    ap.add_argument("--report-dir")
    ap.add_argument("--account-nav", type=float)
    ap.add_argument("--max-trade-risk-pct", type=float)
    ap.add_argument("--max-trade-bp-pct", type=float)
    ap.add_argument("--max-single-ticker-pct", type=float)
    ap.add_argument("--max-portfolio-delta-abs", type=float)
    ap.add_argument("--min-score", type=float)
    ap.add_argument("--min-liquidity-score", type=float)
    ap.add_argument("--min-pop-pct", type=float)
    ap.add_argument("--alert-min-score", type=float)
    ap.add_argument("--profit-target-pct", type=float)
    ap.add_argument("--dte-warning", type=int)
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--skip-discovery", action="store_true")
    ap.add_argument("--skip-risk", action="store_true")
    ap.add_argument("--skip-brief", action="store_true")
    ap.add_argument("--skip-alerts", action="store_true")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--send", action="store_true", help="Send the daily brief instead of dry-run printing it")
    ap.add_argument("--dry-run", action="store_true", help="Print planned commands without running live API steps")
    args = ap.parse_args()

    cfg = load_config(args.config)
    run_dir = ensure_report_dir(args.report_dir)
    analytics_report = run_dir / "analytics.json"
    feedback_report = run_dir / "feedback.json"
    discovery_report = run_dir / "discovery.json"
    scan_report = run_dir / "scan.json"
    risk_report = run_dir / "risk.json"
    plan_report = run_dir / "plan.json"
    manifest_path = run_dir / "manifest.json"
    watchlist = args.watchlist or cfg["watchlists"].get(args.watchlist_name)

    manifest: dict[str, Any] = {
        "created_at": datetime.now().isoformat(),
        "run_dir": str(run_dir),
        "reports": {
            "analytics": str(analytics_report),
            "feedback": str(feedback_report),
            "discovery": str(discovery_report) if not args.skip_discovery else None,
            "scan": str(scan_report),
            "risk": str(risk_report) if not args.skip_risk else None,
            "plan": str(plan_report),
            "brief": str(run_dir / "brief.out") if not args.skip_brief else None,
            "alerts": str(run_dir / "alerts.json") if not args.skip_alerts else None,
            "tickets": str(run_dir / "tickets.json"),
            "operator_summary": str(run_dir / "operator_summary.md"),
            "dashboard": str(run_dir / "dashboard.html"),
        },
        "steps": [],
    }

    analytics_meta = run_command("analytics", build_analytics_cmd(cfg, args), run_dir, dry_run=args.dry_run)
    manifest["steps"].append(analytics_meta)
    if not args.dry_run and analytics_meta["returncode"] == 0:
        analytics_report.write_text(Path(analytics_meta["stdout"]).read_text())

    feedback_meta = run_command("feedback", build_feedback_cmd(cfg, args), run_dir, dry_run=args.dry_run)
    manifest["steps"].append(feedback_meta)
    if not args.dry_run and feedback_meta["returncode"] == 0:
        feedback_report.write_text(Path(feedback_meta["stdout"]).read_text())

    if not args.skip_discovery:
        discovery_meta = run_command("discovery", build_discovery_cmd(cfg, args), run_dir, dry_run=args.dry_run)
        manifest["steps"].append(discovery_meta)
        if not args.dry_run and discovery_meta["returncode"] == 0:
            discovery_report.write_text(Path(discovery_meta["stdout"]).read_text())

    scan_cmd = build_scan_cmd(cfg, args, scan_report)
    manifest["steps"].append(run_command("scan", scan_cmd, run_dir, dry_run=args.dry_run))

    risk_path_for_plan = None
    if not args.skip_risk:
        risk_cmd = build_risk_cmd(args, watchlist)
        risk_meta = run_command("risk", risk_cmd, run_dir, dry_run=args.dry_run)
        manifest["steps"].append(risk_meta)
        if not args.dry_run and risk_meta["returncode"] == 0:
            risk_report.write_text(Path(risk_meta["stdout"]).read_text())
            risk_path_for_plan = risk_report

    if args.dry_run:
        plan_cmd = build_plan_cmd(cfg, args, scan_report, None if args.skip_risk else risk_report)
        manifest["steps"].append(run_command("plan", plan_cmd, run_dir, dry_run=True))
    else:
        plan_cmd = build_plan_cmd(cfg, args, scan_report, risk_path_for_plan)
        plan_meta = run_command("plan", plan_cmd, run_dir, dry_run=False)
        manifest["steps"].append(plan_meta)
        if plan_meta["returncode"] == 0:
            plan_report.write_text(Path(plan_meta["stdout"]).read_text())

    alerts_report = run_dir / "alerts.json"
    if not args.skip_alerts:
        if args.dry_run:
            alerts_cmd = build_alerts_cmd(cfg, args, plan_report)
            manifest["steps"].append(run_command("alerts", alerts_cmd, run_dir, dry_run=True))
        elif plan_report.exists():
            alerts_cmd = build_alerts_cmd(cfg, args, plan_report)
            alerts_meta = run_command("alerts", alerts_cmd, run_dir, dry_run=False)
            manifest["steps"].append(alerts_meta)
            if alerts_meta["returncode"] == 0:
                alerts_report.write_text(Path(alerts_meta["stdout"]).read_text())

    tickets_report = run_dir / "tickets.json"
    if args.dry_run:
        manifest["steps"].append(run_command("tickets", build_tickets_cmd(plan_report), run_dir, dry_run=True))
    elif plan_report.exists():
        tickets_meta = run_command("tickets", build_tickets_cmd(plan_report), run_dir, dry_run=False)
        manifest["steps"].append(tickets_meta)
        if tickets_meta["returncode"] == 0:
            tickets_report.write_text(Path(tickets_meta["stdout"]).read_text())

    if not args.skip_brief:
        brief_cmd = build_brief_cmd(args, watchlist)
        manifest["steps"].append(run_command("brief", brief_cmd, run_dir, dry_run=args.dry_run))

    if args.dry_run:
        manifest["steps"].append(run_command("operator_summary", build_operator_summary_cmd(run_dir), run_dir, dry_run=True))
        manifest["steps"].append(run_command("dashboard", build_dashboard_cmd(run_dir), run_dir, dry_run=True))
        write_json(manifest_path, manifest)
    else:
        write_json(manifest_path, manifest)
        summary_meta = run_command("operator_summary", build_operator_summary_cmd(run_dir), run_dir, dry_run=False)
        manifest["steps"].append(summary_meta)
        dashboard_meta = run_command("dashboard", build_dashboard_cmd(run_dir), run_dir, dry_run=False)
        manifest["steps"].append(dashboard_meta)
        write_json(manifest_path, manifest)

    print(f"\nDaily workflow report dir: {run_dir}")
    for name, path in manifest["reports"].items():
        if path:
            print(f"  {name}: {path}")
    print(f"  manifest: {manifest_path}")
    if args.dry_run:
        print("\nDry run only; no live steps executed.")


if __name__ == "__main__":
    main()
