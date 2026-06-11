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
    _classify_ivr,
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

    def test_executable_message_holds_ivr_below_fifty(self):
        # Regression: SCAN_DISCREPANCIES_2026-06-11 item #5 — the executable
        # scan promoted NVDA (IVR 43) and MSFT (IVR 45) bull puts despite
        # the brief classifying both as "below-median (cautious sell)".
        # Fix: when iv_ranks is provided, tickets whose ticker's IVR is
        # below 50 are demoted from EXECUTABLE into a "HELD BY IVR" line
        # with the reason rendered. Mirrors the existing portfolio-level
        # IVRank guard in portfolio_allocator.py:264 ("half size until
        # ... portfolio IVRank > 50").
        tickets = [
            {
                "decision": "APPROVE", "ticker": "QQQ", "strategy": "BULL_PUT",
                "expiration": "2026-07-17", "strikes": "685/682",
                "limit_credit": 0.88, "do_not_chase_below": 0.81, "score": 63.3,
            },
            {
                "decision": "APPROVE", "ticker": "NVDA", "strategy": "BULL_PUT",
                "expiration": "2026-07-17", "strikes": "195/190",
                "limit_credit": 1.20, "do_not_chase_below": 1.10, "score": 57.3,
            },
            {
                "decision": "APPROVE", "ticker": "MSFT", "strategy": "BULL_PUT",
                "expiration": "2026-07-17", "strikes": "375/370",
                "limit_credit": 1.36, "do_not_chase_below": 1.24, "score": 62.1,
            },
        ]
        message = compose_executable_message(
            timestamp="2026-06-11 10:30 ET",
            regime="AGGRESSIVE",
            sizing_mode="aggressive",
            management={},
            tickets={"tickets": tickets},
            report_dir=Path("/reports/run"),
            iv_ranks={"QQQ": 88.0, "NVDA": 43.0, "MSFT": 45.0},
        )
        # QQQ (IVR 88, above-median) stays in EXECUTABLE
        self.assertIn("EXECUTABLE", message)
        self.assertRegex(
            message,
            r"EXECUTABLE \([01] approved\):",
            "EXECUTABLE header should show only the surviving approved count",
        )
        self.assertIn("QQQ BULL_PUT 2026-07-17 685/682", message)
        # NVDA + MSFT (IVR < 50) are demoted to HELD BY IVR
        self.assertIn("HELD BY IVR", message)
        held_block = message.split("HELD BY IVR", 1)[1]
        # The held block lists both tickers with their IVRs and the regime.
        self.assertIn("NVDA", held_block)
        self.assertIn("MSFT", held_block)
        self.assertIn("43", held_block, "NVDA IVR 43 should appear in held block")
        self.assertIn("45", held_block, "MSFT IVR 45 should appear in held block")
        self.assertIn("cautious", held_block.lower(), "regime annotation should mention cautious")

    def test_executable_message_keeps_all_when_no_iv_ranks(self):
        # Backward compat: callers that don't supply iv_ranks (or whose
        # lookup returns nothing) get the old behavior — every APPROVE
        # ticket stays in EXECUTABLE. Important for the dev path and
        # for runs where the IVR fetch fails.
        tickets = [
            {
                "decision": "APPROVE", "ticker": "NVDA", "strategy": "BULL_PUT",
                "expiration": "2026-07-17", "strikes": "195/190",
                "limit_credit": 1.20, "do_not_chase_below": 1.10, "score": 57.3,
            },
        ]
        message = compose_executable_message(
            timestamp="2026-06-11 10:30 ET",
            regime="AGGRESSIVE",
            sizing_mode="aggressive",
            management={},
            tickets={"tickets": tickets},
            report_dir=Path("/reports/run"),
        )
        self.assertIn("EXECUTABLE (1 approved):", message)
        self.assertNotIn("HELD BY IVR", message)

    def test_executable_message_ignores_tickers_missing_from_iv_ranks(self):
        # If a ticket's ticker isn't in the iv_ranks dict (data unavailable
        # for that symbol), it stays in EXECUTABLE — we don't silently
        # demote on missing data.
        tickets = [
            {
                "decision": "APPROVE", "ticker": "QQQ", "strategy": "BULL_PUT",
                "expiration": "2026-07-17", "strikes": "685/682",
                "limit_credit": 0.88, "do_not_chase_below": 0.81, "score": 63.3,
            },
            {
                "decision": "APPROVE", "ticker": "NEWCO", "strategy": "BULL_PUT",
                "expiration": "2026-07-17", "strikes": "50/45",
                "limit_credit": 0.50, "do_not_chase_below": 0.45, "score": 70.0,
            },
        ]
        message = compose_executable_message(
            timestamp="2026-06-11 10:30 ET",
            regime="AGGRESSIVE",
            sizing_mode="aggressive",
            management={},
            tickets={"tickets": tickets},
            report_dir=Path("/reports/run"),
            iv_ranks={"QQQ": 88.0},  # NEWCO missing
        )
        self.assertIn("EXECUTABLE (2 approved):", message)
        self.assertIn("NEWCO", message)
        self.assertNotIn("HELD BY IVR", message)

    def test_ivr_classifier_matches_iv_rank_module(self):
        # The local _classify_ivr in hermes_ops must stay in lockstep with
        # iv_rank.classify_iv_regime so the HELD BY IVR annotation matches
        # the brief's wording. Test the band boundaries on both sides.
        from iv_rank import classify_iv_regime

        for rank in (0, 10, 24.99, 25, 30, 49.99, 50, 60, 74.99, 75, 90, None):
            self.assertEqual(
                _classify_ivr(rank),
                classify_iv_regime(rank),
                f"band mismatch at rank={rank}",
            )
