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

from datetime import date

import common
from common import atomic_write_json, state_lock
from alerts import build_alerts
from data_reliability import hard_quote_issues, option_leg_issues, quote_issues
from mark_to_market import mark_open_trades, mark_trade, parse_strikes, trade_legs
from position_management import build_management_report
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


class PositionManagementTests(unittest.TestCase):
    TODAY = date(2026, 6, 10)

    def manage(self, *trades):
        report = build_management_report({"trades": list(trades)}, today=self.TODAY)
        return {row["trade_id"]: row for row in report["actions"]}, report

    def test_take_profit_and_stop_loss_close_signals(self):
        rows, _ = self.manage(
            open_trade(id="WIN", expiration="2026-07-17", unrealized_pnl_pct=62.0),
            open_trade(id="BROKEN", expiration="2026-07-17", unrealized_pnl_pct=-230.0),
            open_trade(id="OK", expiration="2026-07-17", unrealized_pnl_pct=10.0),
        )
        self.assertEqual(rows["WIN"]["action"], "CLOSE")
        self.assertIn("TAKE_PROFIT", rows["WIN"]["reasons"][0])
        self.assertEqual(rows["BROKEN"]["action"], "CLOSE")
        self.assertIn("STOP_LOSS", rows["BROKEN"]["reasons"][0])
        self.assertEqual(rows["OK"]["action"], "HOLD")

    def test_dte_management_and_urgency_escalation(self):
        rows, _ = self.manage(
            open_trade(id="GAMMA", expiration="2026-06-25", unrealized_pnl_pct=20.0),  # 15 DTE
            open_trade(id="URGENT", expiration="2026-06-15", unrealized_pnl_pct=20.0),  # 5 DTE
        )
        self.assertEqual(rows["GAMMA"]["action"], "ROLL_OR_CLOSE")
        self.assertEqual(rows["GAMMA"]["urgency"], "MEDIUM")
        self.assertEqual(rows["URGENT"]["action"], "ROLL_OR_CLOSE")
        self.assertEqual(rows["URGENT"]["urgency"], "HIGH")

    def test_profit_target_wins_over_dte_rule(self):
        rows, _ = self.manage(
            open_trade(id="BOTH", expiration="2026-06-25", unrealized_pnl_pct=55.0),  # 15 DTE + target
        )
        self.assertEqual(rows["BOTH"]["action"], "CLOSE")
        self.assertIn("TAKE_PROFIT", rows["BOTH"]["reasons"][0])

    def test_unmarked_or_unparseable_trades_are_review_not_hold(self):
        unmarked = open_trade(id="NOMARK", expiration="2026-08-21")
        no_data = open_trade(id="NODATA", expiration="")
        rows, report = self.manage(unmarked, no_data)
        self.assertEqual(rows["NOMARK"]["action"], "REVIEW")
        self.assertEqual(rows["NODATA"]["action"], "REVIEW")
        self.assertEqual(report["summary"]["review"], 2)

    def test_closed_trades_are_ignored_and_high_urgency_sorts_first(self):
        _, report = self.manage(
            open_trade(id="HOLD", expiration="2026-08-21", unrealized_pnl_pct=5.0),
            open_trade(id="WIN", expiration="2026-08-21", unrealized_pnl_pct=70.0),
            open_trade(id="DONE", status="CLOSED", realized_pnl=50.0),
        )
        self.assertEqual(report["summary"]["open_trades"], 2)
        self.assertEqual(report["actions"][0]["trade_id"], "WIN")


class DataQualityGateTests(unittest.TestCase):
    def test_quote_issues_flags_broken_and_suspect_quotes(self):
        self.assertEqual(quote_issues({"last": 480.0, "bid": 479.9, "ask": 480.1}), [])
        self.assertIn("non-positive last", quote_issues({"last": 0.0}))
        self.assertIn("crossed market (bid > ask)", quote_issues({"last": 480.0, "bid": 481.0, "ask": 480.0}))
        self.assertIn("negative bid", quote_issues({"last": 480.0, "bid": -1.0, "ask": 480.1}))
        self.assertIn("stale quote", quote_issues({"last": 480.0, "stale": True}))
        diverged = quote_issues({"last": 480.0}, reference_price=400.0)
        self.assertTrue(any("diverges" in issue for issue in diverged))
        # 10% default tolerance: a 5% move vs reference close is fine
        self.assertEqual(quote_issues({"last": 420.0}, reference_price=400.0), [])

    def test_hard_quote_issues_excludes_warnings(self):
        issues = quote_issues({"last": -1.0, "stale": True}, reference_price=400.0)
        hard = hard_quote_issues(issues)
        self.assertEqual(hard, ["non-positive last"])

    def test_option_leg_issues(self):
        self.assertEqual(option_leg_issues(1.0, 1.2, iv=0.35), [])
        self.assertIn("crossed market", option_leg_issues(1.5, 1.2))
        self.assertIn("negative quote", option_leg_issues(-0.5, 1.2))
        self.assertIn("implausible IV", option_leg_issues(1.0, 1.2, iv=9.0))
        self.assertIn("implausible IV", option_leg_issues(1.0, 1.2, iv=0.0))
        self.assertEqual(option_leg_issues(0.0, 1.2), [])  # no bid is a side-specific concern

    def test_screens_reject_unsellable_zero_bid_short_legs(self):
        from options_screener import screen_bull_put, screen_csp

        def put_leg(strike, bid, ask, delta):
            return {
                "bid": bid, "ask": ask, "last": (bid + ask) / 2, "mark": (bid + ask) / 2,
                "volume": 500, "open_interest": 1000, "osi": f"SPY260717P{int(strike*1000):08d}",
                "side": "put", "delta": delta, "iv": 0.30,
            }

        chain = {"puts": {
            470.0: put_leg(470.0, 0.0, 2.4, -0.30),   # no bid: mid is fiction
            465.0: put_leg(465.0, 1.0, 1.2, -0.30),   # healthy
            460.0: put_leg(460.0, 0.4, 0.6, -0.20),   # spread long-wing candidate
            455.0: put_leg(455.0, 0.2, 0.4, -0.12),
        }, "calls": {}}

        csp = screen_csp(chain, spot=480.0, dte=35, target_delta=-0.30, min_oi=50)
        strikes = [row["strike"] for row in csp]
        self.assertNotIn(470.0, strikes)  # zero-bid short leg rejected
        self.assertIn(465.0, strikes)

        spreads = screen_bull_put(chain, spot=480.0, dte=35, short_delta=-0.20, wing_width=5.0, min_oi=50)
        self.assertTrue(all(row["short_strike"] != 470.0 for row in spreads))


if __name__ == "__main__":
    unittest.main()
