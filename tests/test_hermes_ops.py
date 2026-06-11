import tempfile
import unittest
from datetime import date
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from hermes_ops import (
    compose_executable_message,
    compose_planning_message,
    planning_brief,
    read_planning_pointer,
    write_run_pointer,
)


class HermesOpsTests(unittest.TestCase):
    def test_planning_pointer_is_atomic_and_same_day_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "reports" / "20260611-083000"
            run_dir.mkdir(parents=True)
            brief = run_dir / "morning_brief.txt"
            brief.write_text("Morning brief")
            pointer = root / "state" / "latest-planning-run.json"
            write_run_pointer(
                pointer,
                {
                    "profile": "planning",
                    "created_at": "2026-06-11T08:40:00",
                    "run_dir": str(run_dir),
                    "brief": str(brief),
                },
            )
            self.assertEqual(planning_brief(pointer, today=date(2026, 6, 11)), "Morning brief")
            self.assertIsNone(read_planning_pointer(pointer, today=date(2026, 6, 12)))
            self.assertEqual(list(pointer.parent.glob(f".{pointer.name}.*.tmp")), [])

    def test_planning_message_golden_and_bounded(self):
        message = compose_planning_message(
            "Morning brief",
            {
                "summary": {"total": 1, "high": 1, "medium": 0, "low": 0},
                "alerts": [{"priority": "HIGH", "title": "SPY", "detail": "Position exception"}],
            },
            Path("/reports/run"),
        )
        self.assertEqual(
            message,
            "Morning brief\nALERTS (1; H:1 M:0 L:0)\n"
            "  [HIGH] SPY: Position exception",
        )
        bounded = compose_planning_message("x" * 200, {}, Path("/reports/run"), limit=80)
        self.assertLessEqual(len(bounded), 80)
        self.assertIn("truncated", bounded)

    def test_executable_message_golden_sections(self):
        message = compose_executable_message(
            timestamp="2026-06-11 10:30 ET",
            regime="CAUTIOUS",
            sizing_mode="cautious",
            management={
                "summary": {
                    "open_trades": 1,
                    "close": 0,
                    "roll_or_close": 1,
                    "review": 0,
                    "hold": 0,
                },
                "actions": [
                    {
                        "ticker": "SPY",
                        "strategy": "CSP",
                        "dte": 12,
                        "unrealized_pnl_pct": -20,
                        "action": "ROLL_OR_CLOSE",
                        "reasons": ["STRIKE_THREAT"],
                    }
                ],
            },
            tickets={
                "tickets": [
                    {
                        "decision": "APPROVE",
                        "ticker": "QQQ",
                        "strategy": "BULL_PUT",
                        "expiration": "2026-07-17",
                        "strikes": "475/470",
                        "limit_credit": 1.2,
                        "do_not_chase_below": 1.0,
                        "score": 75,
                    }
                ]
            },
            report_dir=Path("/reports/run"),
        )
        self.assertIn("10:30 EXECUTABLE SCAN - 2026-06-11 10:30 ET", message)
        self.assertIn("OPEN POSITIONS (1): 0 close, 1 roll", message)
        self.assertIn("QQQ BULL_PUT 2026-07-17 475/470", message)

    def test_executable_message_labels_truncated_candidate_list(self):
        tickets = [
            {
                "decision": "APPROVE",
                "ticker": ticker,
                "strategy": "BULL_PUT",
                "expiration": "2026-07-17",
                "strikes": "100/95",
            }
            for ticker in ("AAPL", "MSFT", "NVDA", "QQQ")
        ]
        message = compose_executable_message(
            timestamp="2026-06-11 10:30 ET",
            regime="CAUTIOUS",
            sizing_mode="cautious",
            management={},
            tickets={"tickets": tickets},
            report_dir=Path("/reports/run"),
        )
        self.assertIn("EXECUTABLE (showing 3 of 4 approved)", message)
