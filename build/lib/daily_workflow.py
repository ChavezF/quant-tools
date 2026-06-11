#!/usr/bin/env python3.12
"""
daily_workflow.py - run the saved daily quant workflow.

Pipeline:
  analytics -> feedback -> validation -> drift -> discovery -> scan -> risk -> scenario stress -> plan ->
  portfolio allocation -> alerts -> tickets -> storage/reconciliation -> execution analytics -> brief ->
  operator summary -> dashboard

Each run writes into reports/YYYYMMDD-HHMMSS/ so the morning process is
repeatable and auditable.
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from common import PROJECT_ROOT, SCRIPTS_DIR, read_json, state_lock
from toolkit_config import add_config_argument, load_config, resolve_project_path


PY = os.environ.get("QUANT_PYTHON", sys.executable)

PROFILE_DEFAULTS = {
    "standard": {},
    "planning": {
        "skip_storage": True,
        "skip_tickets": True,
    },
    "executable": {
        "skip_discovery": True,
        "skip_scenario_stress": True,
        "skip_brief": True,
        "skip_validation": True,
        "skip_drift": True,
        "skip_database_maintenance": True,
        "skip_health": True,
        "skip_dashboard": True,
        "skip_scorecard": True,
    },
}


def profile_skips(profile: str, args: argparse.Namespace) -> dict[str, bool]:
    defaults = PROFILE_DEFAULTS[profile]
    names = {
        "skip_mark", "skip_management", "skip_discovery", "skip_risk",
        "skip_scenario_stress", "skip_allocation", "skip_brief", "skip_alerts",
        "skip_storage", "skip_tickets", "skip_validation", "skip_drift",
        "skip_database_maintenance", "skip_health", "skip_dashboard",
        "skip_operator_summary", "skip_scorecard",
    }
    return {
        name: bool(defaults.get(name, False) or getattr(args, name, False))
        for name in names
    }


def opportunity_funnel(run_dir: Path) -> dict[str, int]:
    plan = read_json(run_dir / "plan.json")
    allocation = read_json(run_dir / "allocation.json")
    tickets = read_json(run_dir / "tickets.json")
    lifecycle = read_json(run_dir / "reconciliation.json").get("ticket_lifecycle", {})
    actions = [
        row for row in plan.get("actions", [])
        if row.get("action_decision") in {"APPROVE", "REDUCE"}
    ]
    return {
        "morning_candidates": len(actions),
        "surviving_candidates": int(allocation.get("summary", {}).get("selected", len(actions)) or 0),
        "ready_tickets": int(lifecycle.get("READY", len(tickets.get("tickets", []))) or 0),
        "submitted_orders": sum(
            int(lifecycle.get(status, 0) or 0)
            for status in ("SUBMITTED", "WORKING", "PARTIAL", "FILLED", "OVERFILLED", "REJECTED", "CANCEL_PENDING", "CANCELLED")
        ),
        "filled_orders": sum(int(lifecycle.get(status, 0) or 0) for status in ("FILLED", "OVERFILLED")),
    }


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
        "--db", storage_db_path(cfg),
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


def build_mark_cmd(cfg: dict[str, Any], args: argparse.Namespace) -> list[str]:
    """Mark open journal trades to market so profit-target alerts can fire."""
    return [
        PY,
        str(SCRIPTS_DIR / "mark_to_market.py"),
        "--journal",
        journal_path(cfg, args),
        "--db",
        storage_db_path(cfg),
        "--json",
    ]


def build_management_cmd(cfg: dict[str, Any], args: argparse.Namespace) -> list[str]:
    """Exit/roll review of open trades; runs after mark_to_market."""
    management_cfg = cfg.get("position_management", {})
    return [
        PY,
        str(SCRIPTS_DIR / "position_management.py"),
        *(["--config", args.config] if args.config else []),
        "--journal",
        journal_path(cfg, args),
        "--db",
        storage_db_path(cfg),
        "--profit-target-pct",
        str(args.profit_target_pct if args.profit_target_pct is not None else management_cfg.get("profit_target_pct", 50.0)),
        "--stop-loss-pct",
        str(management_cfg.get("stop_loss_pct", 200.0)),
        "--manage-dte",
        str(management_cfg.get("manage_dte", 21)),
        "--urgent-dte",
        str(management_cfg.get("urgent_dte", 7)),
        "--strike-threat-sigma",
        str(management_cfg.get("strike_threat_sigma", 0.5)),
        "--json",
    ]


def build_analytics_cmd(cfg: dict[str, Any], args: argparse.Namespace) -> list[str]:
    return [
        PY,
        str(SCRIPTS_DIR / "historical_analytics.py"),
        "--journal",
        journal_path(cfg, args),
        "--db",
        storage_db_path(cfg),
        "--json",
    ]


def build_scorecard_cmd(cfg: dict[str, Any], args: argparse.Namespace) -> list[str]:
    return [
        PY,
        str(SCRIPTS_DIR / "model_scorecard.py"),
        "--journal",
        journal_path(cfg, args),
        "--db",
        storage_db_path(cfg),
        "--account-nav",
        str(args.account_nav if args.account_nav is not None else cfg["risk_limits"]["account_nav"]),
        "--json",
    ]


def build_feedback_cmd(cfg: dict[str, Any], args: argparse.Namespace) -> list[str]:
    feedback_cfg = cfg.get("feedback", {})
    return [
        PY,
        str(SCRIPTS_DIR / "feedback_calibration.py"),
        "--journal",
        journal_path(cfg, args),
        "--db",
        storage_db_path(cfg),
        "--current-min-score",
        str(args.min_score if args.min_score is not None else cfg["risk_limits"]["min_score"]),
        "--min-samples",
        str(feedback_cfg.get("min_samples", 5)),
        "--json",
    ]


def build_validation_cmd(cfg: dict[str, Any], args: argparse.Namespace) -> list[str]:
    validation_cfg = cfg.get("validation", {})
    return [
        PY,
        str(SCRIPTS_DIR / "walk_forward_validation.py"),
        "--journal",
        journal_path(cfg, args),
        "--db",
        storage_db_path(cfg),
        "--min-train",
        str(validation_cfg.get("min_train", 10)),
        "--test-window",
        str(validation_cfg.get("test_window", 5)),
        "--min-selected",
        str(validation_cfg.get("min_selected", 3)),
        "--thresholds",
        *[str(value) for value in validation_cfg.get("thresholds", [50, 55, 60, 65, 70, 75])],
        "--json",
    ]


def build_drift_cmd(cfg: dict[str, Any], args: argparse.Namespace) -> list[str]:
    drift_cfg = cfg.get("drift_monitor", {})
    return [
        PY,
        str(SCRIPTS_DIR / "drift_monitor.py"),
        "--journal",
        journal_path(cfg, args),
        "--db",
        storage_db_path(cfg),
        "--recent-window",
        str(drift_cfg.get("recent_window", 10)),
        "--min-baseline",
        str(drift_cfg.get("min_baseline", 10)),
        "--current-min-score",
        str(args.min_score if args.min_score is not None else cfg["risk_limits"]["min_score"]),
        "--min-samples",
        str(cfg.get("feedback", {}).get("min_samples", 5)),
        "--json",
    ]


def build_risk_cmd(args: argparse.Namespace, watchlist: list[str] | None) -> list[str]:
    """Build the portfolio_risk command. Demo mode (`--target-watchlist`) is
    only triggered by the explicit CLI flag — we do NOT auto-fall-back to the
    resolved scan watchlist, because that turns the live morning pipeline into
    a synthetic-100-share simulation and causes pretrade exposure checks to
    reject every real candidate. See pitfall #28 in the skill."""
    cmd = [PY, str(SCRIPTS_DIR / "portfolio_risk.py"), "--json"]
    if args.target_watchlist:
        cmd += ["--target-watchlist", *args.target_watchlist]
    return cmd


def build_plan_cmd(cfg: dict[str, Any], args: argparse.Namespace, scan_report: Path, risk_report: Path | None) -> list[str]:
    risk_cfg = cfg["risk_limits"]
    journal = args.journal or cfg.get("journal", {}).get("path")
    cmd = [
        PY, str(SCRIPTS_DIR / "action_plan.py"),
        *(["--config", args.config] if args.config else []),
        "--candidates", str(scan_report),
        "--db", storage_db_path(cfg),
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


def build_scenario_stress_cmd(cfg: dict[str, Any], risk_report: Path) -> list[str]:
    scenario_cfg = cfg.get("scenario_stress", {})
    cmd = [
        PY,
        str(SCRIPTS_DIR / "scenario_stress.py"),
        "--portfolio",
        str(risk_report),
        "--json",
    ]
    scenarios_path = resolve_project_path(scenario_cfg.get("scenarios_path"))
    if scenarios_path:
        cmd += ["--scenarios", scenarios_path]
    return cmd


def build_allocation_cmd(
    cfg: dict[str, Any],
    args: argparse.Namespace,
    plan_report: Path,
    risk_report: Path | None = None,
) -> list[str]:
    cmd = [
        PY,
        str(SCRIPTS_DIR / "portfolio_allocator.py"),
        *(["--config", args.config] if args.config else []),
        "--plan",
        str(plan_report),
        "--sizing-mode", str(getattr(args, "sizing_mode", "normal") or "normal"),
        "--json",
    ]
    if risk_report:
        cmd += ["--risk", str(risk_report)]
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


def build_alerts_cmd(
    cfg: dict[str, Any],
    args: argparse.Namespace,
    plan_report: Path,
    reconciliation_report: Path | None = None,
) -> list[str]:
    alert_cfg = cfg.get("alerts", {})
    journal = args.journal or cfg.get("journal", {}).get("path")
    cmd = [
        PY, str(SCRIPTS_DIR / "alerts.py"),
        "--plan", str(plan_report),
        "--min-score", str(args.alert_min_score if args.alert_min_score is not None else alert_cfg.get("min_score", 68.0)),
        "--profit-target-pct", str(args.profit_target_pct if args.profit_target_pct is not None else alert_cfg.get("profit_target_pct", 50.0)),
        "--dte-warning", str(args.dte_warning if args.dte_warning is not None else alert_cfg.get("dte_warning", 21)),
        "--validation", str(plan_report.parent / "validation.json"),
        "--drift", str(plan_report.parent / "drift.json"),
        "--json",
    ]
    if journal:
        cmd += ["--journal", resolve_project_path(journal)]
    cmd += ["--db", storage_db_path(cfg)]
    if reconciliation_report:
        cmd += ["--reconciliation", str(reconciliation_report)]
    return cmd


def build_tickets_cmd(cfg: dict[str, Any], plan_report: Path) -> list[str]:
    lifecycle_cfg = cfg.get("execution_lifecycle", {})
    cmd = [
        PY,
        str(SCRIPTS_DIR / "execution_tickets.py"),
        "--plan",
        str(plan_report),
        "--db",
        storage_db_path(cfg),
        "--pending-expiry-hours",
        str(lifecycle_cfg.get("pending_expiry_hours", 24)),
        "--partial-review-hours",
        str(lifecycle_cfg.get("partial_review_hours", 4)),
        "--json",
    ]
    if not bool(lifecycle_cfg.get("suppress_duplicate_tickets", True)):
        cmd += ["--allow-duplicates"]
    return cmd


def build_dashboard_cmd(run_dir: Path) -> list[str]:
    return [PY, str(SCRIPTS_DIR / "dashboard.py"), "--report-dir", str(run_dir)]


def build_operator_summary_cmd(run_dir: Path) -> list[str]:
    return [PY, str(SCRIPTS_DIR / "operator_summary.py"), "--report-dir", str(run_dir)]


def build_storage_cmd(
    cfg: dict[str, Any],
    args: argparse.Namespace,
    run_dir: Path,
    risk_report: Path,
    tickets_report: Path,
    broker_snapshot_override: Path | None = None,
) -> list[str]:
    storage_cfg = cfg.get("storage", {})
    lifecycle_cfg = cfg.get("execution_lifecycle", {})
    cmd = [
        PY,
        str(SCRIPTS_DIR / "storage_sync.py"),
        "--db",
        resolve_project_path(storage_cfg.get("path")) or str(PROJECT_ROOT / "state" / "quant_tools.db"),
        "--journal",
        journal_path(cfg, args),
        "--tickets",
        str(tickets_report),
        "--portfolio",
        str(risk_report),
        "--pending-expiry-hours",
        str(lifecycle_cfg.get("pending_expiry_hours", 24)),
        "--partial-review-hours",
        str(lifecycle_cfg.get("partial_review_hours", 4)),
        "--apply-assignments",
        "--json",
    ]
    broker_snapshot = broker_snapshot_override or args.broker_snapshot or storage_cfg.get("broker_snapshot")
    if broker_snapshot:
        resolved = str(broker_snapshot) if isinstance(broker_snapshot, Path) else resolve_project_path(broker_snapshot)
        cmd += ["--broker-snapshot", resolved]
    return cmd


def build_public_ingestion_cmd(cfg: dict[str, Any], snapshot_path: Path) -> list[str]:
    ingestion_cfg = cfg.get("public_ingestion", {})
    return [
        PY,
        str(SCRIPTS_DIR / "public_fill_ingestion.py"),
        "--cursor",
        resolve_project_path(ingestion_cfg.get("cursor_path"))
        or str(PROJECT_ROOT / "state" / "public_fill_cursor.json"),
        "--output",
        str(snapshot_path),
        "--page-size",
        str(ingestion_cfg.get("page_size", 100)),
        "--max-pages",
        str(ingestion_cfg.get("max_pages", 100)),
        "--overlap-minutes",
        str(ingestion_cfg.get("overlap_minutes", 15)),
        "--json",
    ]


def build_execution_analytics_cmd(tickets_report: Path, reconciliation_report: Path) -> list[str]:
    return [
        PY,
        str(SCRIPTS_DIR / "execution_analytics.py"),
        "--tickets",
        str(tickets_report),
        "--reconciliation",
        str(reconciliation_report),
        "--json",
    ]


def build_execution_history_cmd(cfg: dict[str, Any]) -> list[str]:
    return [
        PY,
        str(SCRIPTS_DIR / "execution_attribution.py"),
        "--db",
        storage_db_path(cfg),
        "--min-samples",
        str(cfg.get("feedback", {}).get("min_samples", 5)),
        "--json",
    ]


def storage_db_path(cfg: dict[str, Any]) -> str:
    return resolve_project_path(cfg.get("storage", {}).get("path")) or str(PROJECT_ROOT / "state" / "quant_tools.db")


def build_database_maintenance_cmd(cfg: dict[str, Any]) -> list[str]:
    operations = cfg.get("operations", {})
    return [
        PY,
        str(SCRIPTS_DIR / "database_maintenance.py"),
        "--db",
        storage_db_path(cfg),
        "--backup-dir",
        resolve_project_path(operations.get("backup_dir")) or str(PROJECT_ROOT / "state" / "backups"),
        "--retention-days",
        str(operations.get("backup_retention_days", 30)),
        "--keep-last",
        str(operations.get("backup_keep_last", 14)),
        "--json",
    ]


def build_health_cmd(cfg: dict[str, Any]) -> list[str]:
    return [
        PY,
        str(SCRIPTS_DIR / "health_check.py"),
        "--db",
        storage_db_path(cfg),
        "--skip-tests",
        "--json",
    ]


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
    ap.add_argument("--broker-snapshot")
    ap.add_argument("--report-dir")
    ap.add_argument("--account-nav", type=float)
    ap.add_argument("--sizing-mode", choices=["cautious", "normal", "aggressive"],
                    default="normal",
                    help="Scale per-NAV allocation caps: cautious=0.5x, normal=1.0x, aggressive=1.5x")
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
    ap.add_argument("--profile", choices=sorted(PROFILE_DEFAULTS), default="standard")
    ap.add_argument("--skip-mark", action="store_true")
    ap.add_argument("--skip-management", action="store_true")
    ap.add_argument("--skip-discovery", action="store_true")
    ap.add_argument("--skip-risk", action="store_true")
    ap.add_argument("--skip-scenario-stress", action="store_true")
    ap.add_argument("--skip-allocation", action="store_true")
    ap.add_argument("--skip-brief", action="store_true")
    ap.add_argument("--skip-alerts", action="store_true")
    ap.add_argument("--skip-storage", action="store_true")
    ap.add_argument("--skip-tickets", action="store_true")
    ap.add_argument("--skip-validation", action="store_true")
    ap.add_argument("--skip-drift", action="store_true")
    ap.add_argument("--skip-database-maintenance", action="store_true")
    ap.add_argument("--skip-health", action="store_true")
    ap.add_argument("--skip-dashboard", action="store_true")
    ap.add_argument("--skip-operator-summary", action="store_true")
    ap.add_argument("--skip-scorecard", action="store_true")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--send", action="store_true", help="Send the daily brief instead of dry-run printing it")
    ap.add_argument("--dry-run", action="store_true", help="Print planned commands without running live API steps")
    args = ap.parse_args()

    cfg = load_config(args.config)
    skips = profile_skips(args.profile, args)
    workflow_lock = state_lock(f"workflow-{args.profile}")
    workflow_lock.__enter__()
    atexit.register(workflow_lock.__exit__, None, None, None)
    storage_enabled = bool(cfg.get("storage", {}).get("enabled", True)) and not skips["skip_storage"]
    tickets_enabled = not skips["skip_tickets"]
    configured_broker_snapshot = args.broker_snapshot or cfg.get("storage", {}).get("broker_snapshot")
    public_ingestion_enabled = (
        storage_enabled
        and bool(cfg.get("public_ingestion", {}).get("enabled", False))
        and not configured_broker_snapshot
    )
    operations = cfg.get("operations", {})
    backup_enabled = (
        storage_enabled
        and bool(operations.get("backup_on_operator", True))
        and not skips["skip_database_maintenance"]
    )
    health_enabled = bool(operations.get("health_check_on_operator", True)) and not skips["skip_health"]
    scenario_enabled = (
        bool(cfg.get("scenario_stress", {}).get("enabled", True))
        and not skips["skip_scenario_stress"]
        and not skips["skip_risk"]
    )
    allocation_enabled = bool(cfg.get("portfolio_allocation", {}).get("enabled", True)) and not skips["skip_allocation"]
    mark_enabled = bool(cfg.get("journal", {}).get("mark_to_market", True)) and not skips["skip_mark"]
    management_enabled = bool(cfg.get("position_management", {}).get("enabled", True)) and not skips["skip_management"]
    validation_enabled = bool(cfg.get("validation", {}).get("enabled", True)) and not skips["skip_validation"]
    drift_enabled = bool(cfg.get("drift_monitor", {}).get("enabled", True)) and not skips["skip_drift"]
    run_dir = ensure_report_dir(args.report_dir)
    mark_report = run_dir / "mark_to_market.json"
    management_report = run_dir / "management.json"
    analytics_report = run_dir / "analytics.json"
    scorecard_report = run_dir / "scorecard.json"
    feedback_report = run_dir / "feedback.json"
    validation_report = run_dir / "validation.json"
    drift_report = run_dir / "drift.json"
    discovery_report = run_dir / "discovery.json"
    scan_report = run_dir / "scan.json"
    risk_report = run_dir / "risk.json"
    scenario_report = run_dir / "scenario_stress.json"
    plan_report = run_dir / "plan.json"
    allocation_report = run_dir / "allocation.json"
    public_snapshot_report = run_dir / "public_broker_snapshot.json"
    manifest_path = run_dir / "manifest.json"
    watchlist = args.watchlist or cfg["watchlists"].get(args.watchlist_name)

    manifest: dict[str, Any] = {
        "created_at": datetime.now().isoformat(),
        "profile": args.profile,
        "run_dir": str(run_dir),
        "reports": {
            "mark_to_market": str(mark_report) if mark_enabled else None,
            "management": str(management_report) if management_enabled else None,
            "analytics": str(analytics_report),
            "scorecard": str(scorecard_report) if not skips["skip_scorecard"] else None,
            "feedback": str(feedback_report),
            "validation": str(validation_report) if validation_enabled else None,
            "drift": str(drift_report) if drift_enabled else None,
            "discovery": str(discovery_report) if not skips["skip_discovery"] else None,
            "scan": str(scan_report),
            "risk": str(risk_report) if not skips["skip_risk"] else None,
            "scenario_stress": str(scenario_report) if scenario_enabled else None,
            "plan": str(plan_report),
            "allocation": str(allocation_report) if allocation_enabled else None,
            "brief": str(run_dir / "brief.out") if not skips["skip_brief"] else None,
            "alerts": str(run_dir / "alerts.json") if not skips["skip_alerts"] else None,
            "tickets": str(run_dir / "tickets.json") if tickets_enabled else None,
            "broker_snapshot": (
                str(public_snapshot_report)
                if public_ingestion_enabled
                else resolve_project_path(configured_broker_snapshot)
            ),
            "reconciliation": str(run_dir / "reconciliation.json") if storage_enabled else None,
            "execution_analytics": str(run_dir / "execution_analytics.json") if storage_enabled else None,
            "execution_history": str(run_dir / "execution_history.json") if storage_enabled else None,
            "database_maintenance": str(run_dir / "database_maintenance.json") if backup_enabled else None,
            "health": str(run_dir / "health.json") if health_enabled else None,
            "operator_summary": str(run_dir / "operator_summary.md") if not skips["skip_operator_summary"] else None,
            "dashboard": str(run_dir / "dashboard.html") if not skips["skip_dashboard"] else None,
        },
        "steps": [],
    }

    # Refresh unrealized P&L on open trades first so the alerts step (and the
    # operator) see live profit-target status instead of last run's marks.
    if mark_enabled:
        mark_meta = run_command("mark_to_market", build_mark_cmd(cfg, args), run_dir, dry_run=args.dry_run)
        manifest["steps"].append(mark_meta)
        if not args.dry_run and mark_meta["returncode"] == 0:
            mark_report.write_text(Path(mark_meta["stdout"]).read_text())

    if management_enabled:
        management_meta = run_command("management", build_management_cmd(cfg, args), run_dir, dry_run=args.dry_run)
        manifest["steps"].append(management_meta)
        if not args.dry_run and management_meta["returncode"] == 0:
            management_report.write_text(Path(management_meta["stdout"]).read_text())

    analytics_meta = run_command("analytics", build_analytics_cmd(cfg, args), run_dir, dry_run=args.dry_run)
    manifest["steps"].append(analytics_meta)
    if not args.dry_run and analytics_meta["returncode"] == 0:
        analytics_report.write_text(Path(analytics_meta["stdout"]).read_text())

    if not skips["skip_scorecard"]:
        scorecard_meta = run_command("scorecard", build_scorecard_cmd(cfg, args), run_dir, dry_run=args.dry_run)
        manifest["steps"].append(scorecard_meta)
        if not args.dry_run and scorecard_meta["returncode"] == 0:
            scorecard_report.write_text(Path(scorecard_meta["stdout"]).read_text())

    feedback_meta = run_command("feedback", build_feedback_cmd(cfg, args), run_dir, dry_run=args.dry_run)
    manifest["steps"].append(feedback_meta)
    if not args.dry_run and feedback_meta["returncode"] == 0:
        feedback_report.write_text(Path(feedback_meta["stdout"]).read_text())

    if validation_enabled:
        validation_meta = run_command("validation", build_validation_cmd(cfg, args), run_dir, dry_run=args.dry_run)
        manifest["steps"].append(validation_meta)
        if not args.dry_run and validation_meta["returncode"] == 0:
            validation_report.write_text(Path(validation_meta["stdout"]).read_text())

    if drift_enabled:
        drift_meta = run_command("drift", build_drift_cmd(cfg, args), run_dir, dry_run=args.dry_run)
        manifest["steps"].append(drift_meta)
        if not args.dry_run and drift_meta["returncode"] == 0:
            drift_report.write_text(Path(drift_meta["stdout"]).read_text())

    if not skips["skip_discovery"]:
        discovery_meta = run_command("discovery", build_discovery_cmd(cfg, args), run_dir, dry_run=args.dry_run)
        manifest["steps"].append(discovery_meta)
        if not args.dry_run and discovery_meta["returncode"] == 0:
            discovery_report.write_text(Path(discovery_meta["stdout"]).read_text())

    scan_cmd = build_scan_cmd(cfg, args, scan_report)
    manifest["steps"].append(run_command("scan", scan_cmd, run_dir, dry_run=args.dry_run))

    risk_path_for_plan = None
    if not skips["skip_risk"]:
        risk_cmd = build_risk_cmd(args, watchlist)
        risk_meta = run_command("risk", risk_cmd, run_dir, dry_run=args.dry_run)
        manifest["steps"].append(risk_meta)
        if not args.dry_run and risk_meta["returncode"] == 0:
            risk_report.write_text(Path(risk_meta["stdout"]).read_text())
            risk_path_for_plan = risk_report

    if scenario_enabled and (args.dry_run or risk_path_for_plan):
        scenario_meta = run_command(
            "scenario_stress",
            build_scenario_stress_cmd(cfg, risk_report),
            run_dir,
            dry_run=args.dry_run,
        )
        manifest["steps"].append(scenario_meta)
        if not args.dry_run and scenario_meta["returncode"] == 0:
            scenario_report.write_text(Path(scenario_meta["stdout"]).read_text())

    if args.dry_run:
        plan_cmd = build_plan_cmd(cfg, args, scan_report, None if skips["skip_risk"] else risk_report)
        manifest["steps"].append(run_command("plan", plan_cmd, run_dir, dry_run=True))
    else:
        plan_cmd = build_plan_cmd(cfg, args, scan_report, risk_path_for_plan)
        plan_meta = run_command("plan", plan_cmd, run_dir, dry_run=False)
        manifest["steps"].append(plan_meta)
        if plan_meta["returncode"] == 0:
            plan_report.write_text(Path(plan_meta["stdout"]).read_text())

    allocation_path_for_tickets = None
    if allocation_enabled and (args.dry_run or plan_report.exists()):
        allocation_meta = run_command(
            "allocation",
            build_allocation_cmd(
                cfg,
                args,
                plan_report,
                risk_report if args.dry_run and not skips["skip_risk"] else risk_path_for_plan,
            ),
            run_dir,
            dry_run=args.dry_run,
        )
        manifest["steps"].append(allocation_meta)
        if not args.dry_run and allocation_meta["returncode"] == 0:
            allocation_report.write_text(Path(allocation_meta["stdout"]).read_text())
            allocation_path_for_tickets = allocation_report

    tickets_report = run_dir / "tickets.json"
    ticket_source = allocation_report if args.dry_run and allocation_enabled else allocation_path_for_tickets or plan_report
    if tickets_enabled and args.dry_run:
        manifest["steps"].append(run_command("tickets", build_tickets_cmd(cfg, ticket_source), run_dir, dry_run=True))
    elif tickets_enabled and plan_report.exists():
        tickets_meta = run_command("tickets", build_tickets_cmd(cfg, ticket_source), run_dir, dry_run=False)
        manifest["steps"].append(tickets_meta)
        if tickets_meta["returncode"] == 0:
            tickets_report.write_text(Path(tickets_meta["stdout"]).read_text())

    reconciliation_report = run_dir / "reconciliation.json"
    if storage_enabled:
        broker_snapshot_override = None
        if public_ingestion_enabled:
            ingestion_meta = run_command(
                "public_ingestion",
                build_public_ingestion_cmd(cfg, public_snapshot_report),
                run_dir,
                dry_run=args.dry_run,
            )
            manifest["steps"].append(ingestion_meta)
            if args.dry_run or ingestion_meta["returncode"] == 0:
                broker_snapshot_override = public_snapshot_report

        storage_cmd = build_storage_cmd(
            cfg,
            args,
            run_dir,
            risk_report,
            tickets_report,
            broker_snapshot_override=broker_snapshot_override,
        )
        storage_meta = run_command("storage", storage_cmd, run_dir, dry_run=args.dry_run)
        manifest["steps"].append(storage_meta)
        if not args.dry_run and storage_meta["returncode"] == 0:
            reconciliation_report.write_text(Path(storage_meta["stdout"]).read_text())

        if args.dry_run or storage_meta["returncode"] == 0:
            execution_report = run_dir / "execution_analytics.json"
            execution_meta = run_command(
                "execution_analytics",
                build_execution_analytics_cmd(tickets_report, reconciliation_report),
                run_dir,
                dry_run=args.dry_run,
            )
            manifest["steps"].append(execution_meta)
            if not args.dry_run and execution_meta["returncode"] == 0:
                execution_report.write_text(Path(execution_meta["stdout"]).read_text())

            history_report = run_dir / "execution_history.json"
            history_meta = run_command(
                "execution_history",
                build_execution_history_cmd(cfg),
                run_dir,
                dry_run=args.dry_run,
            )
            manifest["steps"].append(history_meta)
            if not args.dry_run and history_meta["returncode"] == 0:
                history_report.write_text(Path(history_meta["stdout"]).read_text())

        if backup_enabled and (args.dry_run or storage_meta["returncode"] == 0):
            database_report = run_dir / "database_maintenance.json"
            database_meta = run_command(
                "database_maintenance",
                build_database_maintenance_cmd(cfg),
                run_dir,
                dry_run=args.dry_run,
            )
            manifest["steps"].append(database_meta)
            if not args.dry_run and database_meta["returncode"] == 0:
                database_report.write_text(Path(database_meta["stdout"]).read_text())

    alerts_report = run_dir / "alerts.json"
    if not skips["skip_alerts"]:
        if args.dry_run:
            alerts_cmd = build_alerts_cmd(
                cfg,
                args,
                plan_report,
                reconciliation_report if storage_enabled else None,
            )
            manifest["steps"].append(run_command("alerts", alerts_cmd, run_dir, dry_run=True))
        elif plan_report.exists():
            alerts_cmd = build_alerts_cmd(
                cfg,
                args,
                plan_report,
                reconciliation_report if reconciliation_report.exists() else None,
            )
            alerts_meta = run_command("alerts", alerts_cmd, run_dir, dry_run=False)
            manifest["steps"].append(alerts_meta)
            if alerts_meta["returncode"] == 0:
                alerts_report.write_text(Path(alerts_meta["stdout"]).read_text())

    if health_enabled:
        health_report = run_dir / "health.json"
        health_meta = run_command("health", build_health_cmd(cfg), run_dir, dry_run=args.dry_run)
        manifest["steps"].append(health_meta)
        if not args.dry_run and health_meta["returncode"] == 0:
            health_report.write_text(Path(health_meta["stdout"]).read_text())

    if not skips["skip_brief"]:
        brief_cmd = build_brief_cmd(args, watchlist)
        manifest["steps"].append(run_command("brief", brief_cmd, run_dir, dry_run=args.dry_run))

    manifest["opportunity_funnel"] = opportunity_funnel(run_dir) if not args.dry_run else {}
    if args.dry_run:
        if not skips["skip_operator_summary"]:
            manifest["steps"].append(run_command("operator_summary", build_operator_summary_cmd(run_dir), run_dir, dry_run=True))
        if not skips["skip_dashboard"]:
            manifest["steps"].append(run_command("dashboard", build_dashboard_cmd(run_dir), run_dir, dry_run=True))
        write_json(manifest_path, manifest)
    else:
        write_json(manifest_path, manifest)
        if not skips["skip_operator_summary"]:
            summary_meta = run_command("operator_summary", build_operator_summary_cmd(run_dir), run_dir, dry_run=False)
            manifest["steps"].append(summary_meta)
        if not skips["skip_dashboard"]:
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
