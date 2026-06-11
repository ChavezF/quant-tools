"""Golden regression tests for quant.py CLI forwarding and journal reports.

quant.py is the entry point for the morning cron but CI only smoke-tests
--help, so a typo in an argv-building branch ships silently. These tests pin
the exact forwarded argv for every subcommand (run() is captured in-process,
so no subprocess and no platform dependence) and pin the exact report dicts
the three journal-stats consumers produce for a fixed fixture, so the shared
trade_stats math cannot drift without a test telling you which number moved.

All expectations use config.example.json explicitly so a developer's local
config.json cannot change the goldens.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import quant
from historical_analytics import build_analytics
from performance_profiles import build_profiles
from trade_journal import journal_stats


CONFIG = str(ROOT / "config.example.json")
STATE = ROOT / "state"
DB = str(STATE / "quant_tools.db")
JOURNAL = str(STATE / "trades.json")


def forwarded(*argv: str) -> list[list[str]]:
    """Run quant.main() with run() captured; returns the forwarded commands."""
    captured: list[list[str]] = []

    def fake_run(script: str, *args: str) -> int:
        captured.append([script, *args])
        return 0

    original_run, original_argv = quant.run, sys.argv
    quant.run = fake_run
    sys.argv = ["quant.py", "--config", CONFIG, *argv]
    try:
        quant.main()
    finally:
        quant.run, sys.argv = original_run, original_argv
    return captured


class CliForwardingGoldenTests(unittest.TestCase):
    maxDiff = None

    def assert_forwards(self, argv: list[str], expected: list[str]):
        commands = forwarded(*argv)
        self.assertEqual(len(commands), 1, commands)
        self.assertEqual(commands[0], expected)

    def test_scan(self):
        self.assert_forwards(
            ["scan", "--watchlist", "SPY", "QQQ", "--ranked", "--json"],
            ["options_screener.py", "--config", CONFIG,
             "--watchlist", "SPY", "QQQ", "--strategies", "csp", "cc", "bull_put",
             "--min-dte", "21", "--max-dte", "45", "--target-delta", "0.3",
             "--min-oi", "50", "--max-expirations", "2",
             "--wing-widths", "2.5", "5", "10", "--db", DB, "--ranked", "--json"],
        )

    def test_pretrade_pulls_risk_limits_from_config(self):
        self.assert_forwards(
            ["pretrade", "--candidates", "c.json", "--json"],
            ["pretrade_check.py", "--candidates", "c.json",
             "--account-nav", "30000", "--max-trade-risk-pct", "0.05",
             "--max-trade-bp-pct", "0.2", "--max-single-ticker-pct", "0.25",
             "--max-portfolio-delta-abs", "250", "--min-score", "55",
             "--min-liquidity-score", "45", "--min-pop-pct", "55", "--json"],
        )

    def test_plan_resolves_journal_and_db(self):
        self.assert_forwards(
            ["plan", "--candidates", "c.json", "--json"],
            ["action_plan.py", "--config", CONFIG, "--candidates", "c.json",
             "--account-nav", "30000", "--max-trade-risk-pct", "0.05",
             "--max-trade-bp-pct", "0.2", "--max-single-ticker-pct", "0.25",
             "--max-portfolio-delta-abs", "250", "--min-score", "55",
             "--min-liquidity-score", "45", "--min-pop-pct", "55", "--top", "10",
             "--journal", JOURNAL, "--db", DB, "--json"],
        )

    def test_alerts(self):
        self.assert_forwards(
            ["alerts", "--plan", "plan.json", "--json"],
            ["alerts.py", "--min-score", "68.0", "--profit-target-pct", "50.0",
             "--dte-warning", "21", "--plan", "plan.json",
             "--journal", JOURNAL, "--db", DB, "--json"],
        )

    def test_alerts_forwards_management(self):
        # Regression: `quant.py alerts --management <file>` regressed when the
        # phantom-position guard (fa1e567) added --management to alerts.py
        # main() but the quant.py subparser + argv-forwarder were not updated.
        # Pins the forwarding contract so a future refactor of one without
        # the other is caught.
        self.assert_forwards(
            ["alerts", "--management", "mgmt.json", "--json"],
            ["alerts.py", "--min-score", "68.0", "--profit-target-pct", "50.0",
             "--dte-warning", "21", "--management", "mgmt.json",
             "--journal", JOURNAL, "--db", DB, "--json"],
        )

    def test_analytics(self):
        self.assert_forwards(
            ["analytics", "--json"],
            ["historical_analytics.py", "--journal", JOURNAL, "--db", DB,
             "--recent-window", "10", "--json"],
        )

    def test_feedback(self):
        self.assert_forwards(
            ["feedback", "--json"],
            ["feedback_calibration.py", "--journal", JOURNAL, "--db", DB,
             "--current-min-score", "55", "--min-samples", "5", "--json"],
        )

    def test_validate(self):
        self.assert_forwards(
            ["validate", "--json"],
            ["walk_forward_validation.py", "--journal", JOURNAL, "--db", DB,
             "--min-train", "10", "--test-window", "5", "--min-selected", "3",
             "--thresholds", "50", "55", "60", "65", "70", "75", "--json"],
        )

    def test_drift(self):
        self.assert_forwards(
            ["drift", "--json"],
            ["drift_monitor.py", "--journal", JOURNAL, "--db", DB,
             "--recent-window", "10", "--min-baseline", "10",
             "--current-min-score", "55", "--min-samples", "5", "--json"],
        )

    def test_mark_and_manage(self):
        self.assert_forwards(
            ["mark", "--dry-run", "--json"],
            ["mark_to_market.py", "--journal", JOURNAL, "--db", DB, "--dry-run", "--json"],
        )
        self.assert_forwards(
            ["manage", "--json"],
            ["position_management.py", "--config", CONFIG, "--journal", JOURNAL, "--db", DB,
             "--profit-target-pct", "50.0", "--stop-loss-pct", "200.0",
             "--manage-dte", "21", "--urgent-dte", "7", "--json"],
        )

    def test_tickets_pulls_lifecycle_config(self):
        self.assert_forwards(
            ["tickets", "--plan", "plan.json", "--json"],
            ["execution_tickets.py", "--plan", "plan.json", "--db", DB,
             "--pending-expiry-hours", "24", "--partial-review-hours", "4", "--json"],
        )

    def test_stage(self):
        self.assert_forwards(
            ["stage", "--ticket-id", "QTK-1", "--confirm", "--json"],
            ["order_staging.py", "--ticket-id", "QTK-1", "--db", DB,
             "--journal", JOURNAL, "--confirm", "--json"],
        )

    def test_scorecard(self):
        self.assert_forwards(
            ["scorecard", "--json"],
            ["model_scorecard.py", "--journal", JOURNAL, "--db", DB,
             "--account-nav", "30000", "--json"],
        )

    def test_allocate_forwards_bootstrap_risk_report(self):
        self.assert_forwards(
            ["allocate", "--plan", "plan.json", "--risk", "risk.json", "--json"],
            ["portfolio_allocator.py", "--plan", "plan.json", "--config", CONFIG,
             "--risk", "risk.json", "--json"],
        )

    def test_intraday_sentinel(self):
        self.assert_forwards(
            ["sentinel", "--send", "--json"],
            ["intraday_sentinel.py", "--config", CONFIG, "--journal", JOURNAL,
             "--db", DB, "--send", "--json"],
        )

    def test_storage(self):
        self.assert_forwards(
            ["storage", "--tickets", "t.json", "--json"],
            ["storage_sync.py", "--db", DB, "--journal", JOURNAL,
             "--pending-expiry-hours", "24", "--partial-review-hours", "4",
             "--tickets", "t.json", "--json"],
        )

    def test_reconcile(self):
        self.assert_forwards(
            ["reconcile", "--journal", "j.json", "--tickets", "t.json",
             "--broker-snapshot", "b.json", "--apply-updates", "--json"],
            ["broker_reconciliation.py", "--journal", "j.json", "--tickets", "t.json",
             "--broker-snapshot", "b.json", "--apply-updates", "--json"],
        )

    def test_broker_sync(self):
        self.assert_forwards(
            ["broker-sync", "--json"],
            ["public_fill_ingestion.py",
             "--cursor", str(STATE / "public_fill_cursor.json"),
             "--output", str(STATE / "public_broker_snapshot.json"),
             "--page-size", "100", "--max-pages", "100", "--overlap-minutes", "15",
             "--json"],
        )

    def test_ticket_lifecycle(self):
        self.assert_forwards(
            ["ticket-lifecycle", "--active", "--json"],
            ["ticket_lifecycle.py", "--db", DB, "--active", "--json"],
        )

    def test_execution_history(self):
        self.assert_forwards(
            ["execution-history", "--json"],
            ["execution_attribution.py", "--min-samples", "5", "--db", DB, "--json"],
        )

    def test_verify_and_db_maintenance(self):
        self.assert_forwards(
            ["verify", "--skip-tests", "--json"],
            ["health_check.py", "--db", DB, "--skip-tests", "--json"],
        )
        self.assert_forwards(
            ["db-maintenance", "--vacuum", "--json"],
            ["database_maintenance.py", "--db", DB,
             "--backup-dir", str(STATE / "backups"),
             "--retention-days", "30", "--keep-last", "14", "--vacuum", "--json"],
        )

    def test_backtest_aliases_v2(self):
        self.assert_forwards(
            ["backtest", "--tickers", "NVDA"],
            ["earnings_backtest_v2.py", "--tickers", "NVDA", "--num-events", "8"],
        )

    def test_daily_forwards_flags_and_sizing(self):
        self.assert_forwards(
            ["daily", "--watchlist", "SPY", "--sizing-mode", "cautious",
             "--skip-mark", "--skip-management", "--dry-run"],
            ["daily_workflow.py", "--profile", "standard", "--config", CONFIG,
             "--watchlist", "SPY", "--watchlist-name", "core",
             "--sizing-mode", "cautious", "--top", "10",
             "--skip-mark", "--skip-management", "--dry-run"],
        )

    def test_operator_forwards_skips(self):
        self.assert_forwards(
            ["operator", "--sizing-mode", "aggressive", "--dry-run", "--skip-brief"],
            ["daily_workflow.py", "--profile", "standard", "--config", CONFIG,
             "--sizing-mode", "aggressive", "--dry-run", "--skip-brief"],
        )


def golden_trades() -> list[dict]:
    return [
        {"id": "T1", "status": "CLOSED", "ticker": "SPY", "strategy": "CSP",
         "opened_at": "2026-05-01", "closed_at": "2026-05-10",
         "capital_at_risk": 475.0, "score": 66, "realized_pnl": 120.0},
        {"id": "T2", "status": "CLOSED", "ticker": "SPY", "strategy": "CSP",
         "opened_at": "2026-05-05", "closed_at": "2026-05-18",
         "capital_at_risk": 470.0, "score": 58, "realized_pnl": -260.0},
        {"id": "T3", "status": "CLOSED", "ticker": "QQQ", "strategy": "BULL_PUT",
         "opened_at": "2026-05-12", "closed_at": "2026-05-25",
         "capital_at_risk": 380.0, "score": 71, "realized_pnl": 95.0},
        {"id": "T4", "status": "CLOSED", "ticker": "QQQ", "strategy": "BULL_PUT",
         "opened_at": "2026-05-15", "closed_at": "2026-06-01",
         "capital_at_risk": 380.0, "score": 73, "realized_pnl": 0.0},
        {"id": "T5", "status": "OPEN", "ticker": "NVDA", "strategy": "CC",
         "opened_at": "2026-06-05", "expiration": "2026-07-17"},
    ]


class JournalReportGoldenTests(unittest.TestCase):
    maxDiff = None

    def test_journal_stats_golden(self):
        self.assertEqual(
            journal_stats(golden_trades()),
            {
                "open_trades": 1,
                "closed_trades": 4,
                "total_realized_pnl": -45.0,
                "win_rate": 50.0,
                "avg_pnl": -11.25,
                "profit_factor": 0.83,
                "by_strategy": {
                    "CSP": {"count": 2, "pnl": -140.0, "wins": 1, "win_rate": 50.0},
                    "BULL_PUT": {"count": 2, "pnl": 95.0, "wins": 1, "win_rate": 50.0},
                },
            },
        )

    def test_analytics_overall_golden(self):
        report = build_analytics({"trades": golden_trades()}, recent_window=2)
        self.assertEqual(
            report["overall"],
            {
                "count": 4, "wins": 2, "losses": 2, "win_rate": 50.0,
                "total_pnl": -45.0, "expectancy": -11.25,
                "avg_win": 107.5, "avg_loss": -130.0, "profit_factor": 0.83,
                "avg_return_on_risk_pct": -1.26, "total_capital_at_risk": 1705.0,
            },
        )
        self.assertEqual(
            report["drawdown"],
            {"peak_pnl": 120.0, "ending_pnl": -45.0,
             "max_drawdown": 260.0, "current_drawdown": 165.0},
        )
        self.assertEqual(report["recent"]["count"], 2)
        self.assertEqual(report["recent"]["total_pnl"], 95.0)

    def test_profiles_golden(self):
        profiles = build_profiles(golden_trades())
        self.assertEqual(
            profiles["strategy"]["CSP"],
            {
                "count": 2, "wins": 1, "losses": 1, "pnl": -140.0,
                "gross_wins": 120.0, "gross_losses": 260.0, "avg_pnl": -70.0,
                "win_rate": 50.0, "profit_factor": 0.46, "confidence": "TINY",
                "signal": "INSUFFICIENT",  # n=2 < 3: too little history to throttle on
            },
        )
        self.assertEqual(
            profiles["ticker_strategy"]["QQQ|BULL_PUT"],
            {
                "count": 2, "wins": 1, "losses": 1, "pnl": 95.0,
                "gross_wins": 95.0, "gross_losses": 0.0, "avg_pnl": 47.5,
                "win_rate": 50.0, "profit_factor": None, "confidence": "TINY",
                "signal": "INSUFFICIENT",
            },
        )


if __name__ == "__main__":
    unittest.main()
