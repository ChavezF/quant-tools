import json
import tempfile
import threading
import unittest
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import common
from common import atomic_write_json, state_lock
from alerts import build_alerts
from mark_to_market import mark_open_trades, mark_trade, parse_strikes, trade_legs
from trade_journal import default_state, load_state


def open_trade(**overrides):
    trade = {
        "id": "T20260601-001",
        "ticket_id": "QTK-001",
        "status": "OPEN",
        "ticker": "SPY",
        "strategy": "CSP",
        "expiration": "2026-07-17",
        "strikes": "475",
        "quantity": 1,
        "entry_credit": 2.0,
        "entry_debit": 0.0,
        "capital_at_risk": 475.0,
    }
    trade.update(overrides)
    return trade


def lookup_from(marks):
    """MarkLookup backed by a {(type, strike): mark} dict for one chain."""
    return lambda ticker, expiration, option_type, strike: marks.get((option_type, strike))


class AtomicStateTests(unittest.TestCase):
    def test_atomic_write_json_round_trip_and_no_temp_residue(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "state.json"
            atomic_write_json(path, {"trades": [1, 2, 3]})
            self.assertEqual(json.loads(path.read_text()), {"trades": [1, 2, 3]})
            atomic_write_json(path, {"trades": []})
            self.assertEqual(json.loads(path.read_text()), {"trades": []})
            leftovers = [p for p in path.parent.iterdir() if p.name != "state.json"]
            self.assertEqual(leftovers, [])

    def test_load_state_missing_file_returns_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_state(Path(tmp) / "missing.json"), default_state())

    def test_load_state_corrupt_journal_refuses_loudly(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trades.json"
            path.write_text('{"version": 1, "trades": [{"id": "T1"')  # truncated write
            with self.assertRaises(SystemExit) as ctx:
                load_state(path)
            self.assertIn("corrupt", str(ctx.exception))
            # The corrupt file must survive untouched for manual recovery.
            self.assertIn("T1", path.read_text())


class StateLockTests(unittest.TestCase):
    def setUp(self):
        self._original_state_dir = common.STATE_DIR
        self._tmp = tempfile.TemporaryDirectory()
        common.STATE_DIR = Path(self._tmp.name)

    def tearDown(self):
        common.STATE_DIR = self._original_state_dir
        self._tmp.cleanup()

    def test_lock_is_exclusive_and_released(self):
        lock_path = common.STATE_DIR / ".journal.lock"
        with state_lock("journal", timeout_seconds=0.3):
            self.assertTrue(lock_path.exists())
            with self.assertRaises(SystemExit):
                with state_lock("journal", timeout_seconds=0.3):
                    pass
        self.assertFalse(lock_path.exists())
        with state_lock("journal", timeout_seconds=0.3):
            pass

    def test_stale_lock_is_broken(self):
        lock_path = common.STATE_DIR / ".journal.lock"
        lock_path.write_text("pid=0")
        with state_lock("journal", timeout_seconds=0.3, stale_seconds=0.0):
            self.assertTrue(lock_path.exists())

    def test_waiter_acquires_after_release(self):
        acquired = []

        def holder():
            with state_lock("journal", timeout_seconds=5.0):
                acquired.append("second")

        with state_lock("journal", timeout_seconds=5.0):
            thread = threading.Thread(target=holder)
            thread.start()
            thread.join(timeout=0.1)
            self.assertEqual(acquired, [])
        thread.join(timeout=5.0)
        self.assertEqual(acquired, ["second"])


class TradeLegTests(unittest.TestCase):
    def test_parse_strikes(self):
        self.assertEqual(parse_strikes("475"), [475.0])
        self.assertEqual(parse_strikes("470/475"), [470.0, 475.0])
        self.assertEqual(parse_strikes("440, 445, 505, 510"), [440.0, 445.0, 505.0, 510.0])
        self.assertEqual(parse_strikes(None), [])
        self.assertEqual(parse_strikes("ATM/junk"), [])

    def test_leg_construction_per_strategy(self):
        legs, err = trade_legs(open_trade(strategy="CSP", strikes="475"))
        self.assertIsNone(err)
        self.assertEqual(legs, [{"side": "SHORT", "option_type": "P", "strike": 475.0}])

        legs, _ = trade_legs(open_trade(strategy="CC", strikes="480"))
        self.assertEqual(legs, [{"side": "SHORT", "option_type": "C", "strike": 480.0}])

        legs, _ = trade_legs(open_trade(strategy="BULL_PUT", strikes="470/475"))
        self.assertEqual(
            legs,
            [
                {"side": "SHORT", "option_type": "P", "strike": 475.0},
                {"side": "LONG", "option_type": "P", "strike": 470.0},
            ],
        )

        legs, _ = trade_legs(open_trade(strategy="BEAR_CALL", strikes="505/510"))
        self.assertEqual(
            legs,
            [
                {"side": "SHORT", "option_type": "C", "strike": 505.0},
                {"side": "LONG", "option_type": "C", "strike": 510.0},
            ],
        )

        legs, _ = trade_legs(open_trade(strategy="SHORT_STRANGLE", strikes="450/510"))
        self.assertEqual(
            legs,
            [
                {"side": "SHORT", "option_type": "P", "strike": 450.0},
                {"side": "SHORT", "option_type": "C", "strike": 510.0},
            ],
        )

        legs, _ = trade_legs(open_trade(strategy="IRON_CONDOR", strikes="440/445/505/510"))
        self.assertEqual(
            [(piece["side"], piece["option_type"], piece["strike"]) for piece in legs],
            [("LONG", "P", 440.0), ("SHORT", "P", 445.0), ("SHORT", "C", 505.0), ("LONG", "C", 510.0)],
        )

    def test_unsupported_or_malformed_trades_report_reason(self):
        legs, err = trade_legs(open_trade(strategy="CALENDAR", strikes="475"))
        self.assertIsNone(legs)
        self.assertIn("unsupported", err)

        legs, err = trade_legs(open_trade(strategy="BULL_PUT", strikes="475"))
        self.assertIsNone(legs)
        self.assertIn("expected 2", err)

        legs, err = trade_legs(open_trade(expiration=""))
        self.assertIsNone(legs)
        self.assertIn("expiration", err)


class MarkToMarketTests(unittest.TestCase):
    def test_csp_half_decayed_marks_fifty_percent_of_max_profit(self):
        trade = open_trade(entry_credit=2.0)
        row = mark_trade(trade, lookup_from({("P", 475.0): 1.0}), "2026-06-10T09:00:00")
        self.assertEqual(row["status"], "MARKED")
        self.assertEqual(trade["unrealized_pnl"], 100.0)  # (2.00 - 1.00) * 1 * 100
        self.assertEqual(trade["unrealized_pnl_pct"], 50.0)  # half the credit captured
        self.assertEqual(trade["marked_at"], "2026-06-10T09:00:00")

    def test_bull_put_uses_net_spread_cost_and_quantity(self):
        trade = open_trade(strategy="BULL_PUT", strikes="470/475", entry_credit=1.2, quantity=2)
        marks = {("P", 475.0): 2.0, ("P", 470.0): 1.4}  # cost to close = 0.60
        row = mark_trade(trade, lookup_from(marks), "now")
        self.assertEqual(row["status"], "MARKED")
        self.assertEqual(trade["unrealized_pnl"], 120.0)  # (1.20 - 0.60) * 2 * 100
        self.assertEqual(trade["unrealized_pnl_pct"], 50.0)

    def test_losing_position_marks_negative(self):
        trade = open_trade(entry_credit=2.0)
        row = mark_trade(trade, lookup_from({("P", 475.0): 5.0}), "now")
        self.assertEqual(row["status"], "MARKED")
        self.assertEqual(trade["unrealized_pnl"], -300.0)
        self.assertEqual(trade["unrealized_pnl_pct"], -150.0)

    def test_missing_mark_reports_unmarked_without_mutation(self):
        trade = open_trade(entry_credit=2.0)
        row = mark_trade(trade, lookup_from({}), "now")
        self.assertEqual(row["status"], "UNMARKED")
        self.assertNotIn("unrealized_pnl_pct", trade)

    def test_debit_trades_are_skipped(self):
        trade = open_trade(entry_credit=0.0, entry_debit=1.5)
        row = mark_trade(trade, lookup_from({("P", 475.0): 1.0}), "now")
        self.assertEqual(row["status"], "SKIPPED")
        self.assertIn("net-credit", row["reason"])

    def test_mark_open_trades_only_touches_open_trades(self):
        closed = open_trade(id="T-CLOSED", status="CLOSED", realized_pnl=80.0)
        state = {"trades": [open_trade(entry_credit=2.0), closed]}
        report = mark_open_trades(state, lookup_from({("P", 475.0): 1.0}), now_iso="now")
        self.assertEqual(report["summary"], {"open_trades": 1, "marked": 1, "unmarked": 0, "skipped": 0})
        self.assertNotIn("marked_at", closed)

    def test_marked_journal_fires_profit_target_alert(self):
        # End-to-end for the management rule: mark at 50% decay, alert fires.
        state = {"trades": [open_trade(entry_credit=2.0)]}
        mark_open_trades(state, lookup_from({("P", 475.0): 1.0}))
        report = build_alerts(
            plan=None,
            journal_state=state,
            min_score=68.0,
            profit_target_pct=50.0,
            dte_warning=0,
        )
        kinds = {alert["kind"] for alert in report["alerts"]}
        self.assertIn("profit_target", kinds)

    def test_unmarked_journal_does_not_fire_profit_target_alert(self):
        state = {"trades": [open_trade(entry_credit=2.0)]}
        report = build_alerts(
            plan=None,
            journal_state=state,
            min_score=68.0,
            profit_target_pct=50.0,
            dte_warning=0,
        )
        kinds = {alert["kind"] for alert in report["alerts"]}
        self.assertNotIn("profit_target", kinds)


if __name__ == "__main__":
    unittest.main()
