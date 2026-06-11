import tempfile
import unittest
import sqlite3
from argparse import Namespace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import sys

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from cache_utils import cached, read_cache
from candidate_scoring import score_results
from common import format_osi_symbol, parse_osi_expiration, parse_osi_parts, parse_osi_strike
from data_reliability import quote_is_stale, retry_call, utc_now_iso
from correlation_risk import correlation_penalty
from execution_quality import execution_quality
from execution_analytics import build_execution_analytics
from execution_attribution import (
    adjustment_for_summary,
    build_execution_attribution,
    load_execution_records,
)
from performance_profiles import build_profiles, lookup_profile
from pretrade_check import RiskLimits, evaluate_report
from scan_optimizer import parse_wing_widths, select_expirations
from toolkit_config import deep_merge
from trade_journal import add_trade, close_trade, default_state, journal_stats
from action_plan import apply_performance_overlay, build_action_plan
from adaptive_sizing import adaptive_size
from alerts import build_alerts
from broker_reconciliation import (
    apply_assignment_updates,
    apply_journal_updates,
    build_reconciliation,
    proposed_assignment_updates,
)
from dashboard import build_dashboard
from daily_workflow import build_public_ingestion_cmd, build_storage_cmd, profile_skips
from database_maintenance import backup_database, maintain_database, prune_backups
from drift_monitor import build_drift_report
from execution_tickets import build_ticket_report, build_tickets
from feedback_calibration import build_feedback_report
from historical_analytics import build_analytics
from opportunity_discovery import score_discovery_metrics
from order_staging import build_stage_packet, confirm_journal_stage
from operator_summary import build_summary, main as operator_summary_main
from portfolio_allocator import allocate_portfolio
from public_fill_ingestion import build_snapshot, normalize_fills, normalize_lifecycle_events
from scenario_stress import build_scenario_report
from model_scorecard import build_scorecard
from health_check import build_health_report
from walk_forward_validation import build_walk_forward_report
from storage import (
    apply_ticket_lifecycle,
    apply_lifecycle_policy,
    connect,
    export_journal_state,
    insert_option_chain_snapshot,
    load_active_tickets,
    load_fills_for_reconciliation,
    list_tickets,
    record_reconciliation,
    set_ticket_lifecycle,
    table_counts,
    ticket_lifecycle_counts,
    upsert_equity_lots,
    upsert_fills,
    upsert_tickets,
    upsert_trades,
)
from storage_sync import sync_artifacts


def sample_scan_report():
    return {
        "tickers": {
            "SPY": {
                "metrics": {"rv_21d_pct": 18, "iv_rank_proxy_pct": 62, "earnings": {}},
                "dte": 43,
                "strategies": {
                    "bull_put": [
                        {
                            "strategy": "BULL_PUT",
                            "short_strike": 475,
                            "long_strike": 470,
                            "credit": 1.2,
                            "max_loss": 380,
                            "ratio": 0.315,
                            "pop_pct": 78,
                            "delta_short": -0.22,
                            "ann_roc_pct": 20.4,
                            "volume_short": 80,
                            "open_interest_short": 900,
                        }
                    ],
                    "csp": [
                        {
                            "strategy": "CSP",
                            "strike": 480,
                            "credit": 4.5,
                            "bid": 4.4,
                            "ask": 4.6,
                            "capital": 48000,
                            "delta": -0.25,
                            "pop_pct": 75,
                            "ann_roc_pct": 7.9,
                            "distance_to_strike_pct": 4.0,
                            "volume": 150,
                            "open_interest": 1200,
                        }
                    ],
                },
            }
        }
    }


class CoreWorkflowTests(unittest.TestCase):
    def test_osi_formatting_and_manual_order_staging(self):
        self.assertEqual(
            format_osi_symbol("spy", "2026-07-17", "P", 475),
            "SPY260717P00475000",
        )
        ticket = {
            "ticket_id": "QTK-STAGE",
            "lifecycle_status": "READY",
            "ticker": "SPY",
            "strategy": "CSP",
            "expiration": "2026-07-17",
            "strikes": "475",
            "target_quantity": 1,
            "limit_credit": 1.2,
        }
        packet = build_stage_packet(ticket, "/tmp/place_order.py")
        self.assertEqual(packet["stage_status"], "READY_FOR_MANUAL_SUBMISSION")
        self.assertIn("--symbol SPY260717P00475000", packet["place_order_command"])
        self.assertIn("--limit-price 1.2", packet["place_order_command"])
        with self.assertRaisesRegex(ValueError, "must be READY"):
            build_stage_packet({**ticket, "lifecycle_status": "SUBMITTED"})

        spread = build_stage_packet(
            {
                **ticket,
                "strategy": "BULL_PUT",
                "strikes": "475/470",
            }
        )
        self.assertEqual(spread["stage_status"], "BROKER_HELPER_UNSUPPORTED")
        self.assertIsNone(spread["place_order_command"])
        self.assertEqual(len(spread["manual_order"]["legs"]), 2)

    def test_staging_confirmation_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "quant.db"
            state_path = Path(tmp) / "trades.json"
            ticket = {
                "ticket_id": "QTK-STAGED",
                "lifecycle_status": "READY",
                "ticker": "SPY",
                "strategy": "CSP",
                "expiration": "2026-07-17",
                "strikes": "475",
                "target_quantity": 1,
                "limit_credit": 1.2,
                "capital_required": 47500,
            }
            con = connect(db_path)
            try:
                upsert_tickets(con, [ticket])
            finally:
                con.close()
            first, first_created = confirm_journal_stage(ticket, state_path, str(db_path))
            second, second_created = confirm_journal_stage(ticket, state_path, str(db_path))
            self.assertTrue(first_created)
            self.assertFalse(second_created)
            self.assertEqual(first["id"], second["id"])
            self.assertEqual(second["status"], "STAGED")

    def test_public_history_normalizes_two_leg_bull_put(self):
        transactions = [
            {
                "id": "TX-SHORT",
                "timestamp": "2026-06-10T14:30:00+00:00",
                "type": "TRADE",
                "account_number": "A1",
                "symbol": "SPY260717P00475000",
                "security_type": "OPTION",
                "side": "SELL",
                "principal_amount": 120,
                "quantity": 1,
                "fees": 0.65,
            },
            {
                "id": "TX-LONG",
                "timestamp": "2026-06-10T14:30:00+00:00",
                "type": "TRADE",
                "account_number": "A1",
                "symbol": "SPY260717P00470000",
                "security_type": "OPTION",
                "side": "BUY",
                "principal_amount": -40,
                "quantity": 1,
                "fees": 0.65,
            },
        ]
        fills = normalize_fills(transactions)
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]["strategy"], "BULL_PUT")
        self.assertEqual(fills[0]["ticker"], "SPY")
        self.assertEqual(fills[0]["expiration"], "2026-07-17")
        self.assertEqual(fills[0]["strikes"], "475/470")
        self.assertEqual(fills[0]["net_credit"], 0.8)
        self.assertEqual(len(fills[0]["legs"]), 2)
        self.assertEqual(fills[0]["execution_effect"], "OPEN")
        self.assertEqual(fills[0]["classification_confidence"], "MEDIUM")

    def test_public_history_classifies_closing_bull_put_debit(self):
        transactions = [
            {
                "id": "TX-CLOSE-SHORT",
                "timestamp": "2026-06-10T15:30:00+00:00",
                "type": "TRADE",
                "account_number": "A1",
                "symbol": "SPY260717P00475000",
                "security_type": "OPTION",
                "side": "BUY",
                "principal_amount": -55,
                "quantity": 1,
                "fees": 0.65,
            },
            {
                "id": "TX-CLOSE-LONG",
                "timestamp": "2026-06-10T15:30:00+00:00",
                "type": "TRADE",
                "account_number": "A1",
                "symbol": "SPY260717P00470000",
                "security_type": "OPTION",
                "side": "SELL",
                "principal_amount": 15,
                "quantity": 1,
                "fees": 0.65,
            },
        ]
        fill = normalize_fills(transactions)[0]
        self.assertEqual(fill["strategy"], "BULL_PUT")
        self.assertEqual(fill["strikes"], "475/470")
        self.assertEqual(fill["execution_effect"], "CLOSE")
        self.assertEqual(fill["net_credit"], -0.4)

    def test_public_history_separates_assignment_and_expiration_events(self):
        events = normalize_lifecycle_events(
            [
                {
                    "id": "ASSIGN-1",
                    "timestamp": "2026-06-10T20:00:00+00:00",
                    "type": "POSITION_ADJUSTMENT",
                    "sub_type": "MISC",
                    "symbol": "SPY260717P00475000",
                    "description": "Option assigned",
                    "quantity": 1,
                },
                {
                    "id": "EXPIRE-1",
                    "timestamp": "2026-06-10T20:00:00+00:00",
                    "type": "POSITION_ADJUSTMENT",
                    "sub_type": "MISC",
                    "symbol": "SPY260717P00470000",
                    "description": "Option expired",
                    "quantity": 1,
                },
            ]
        )
        self.assertEqual([event["event_type"] for event in events], ["ASSIGNMENT", "EXPIRATION"])

    def test_public_history_keeps_partial_fills_at_different_times(self):
        base = {
            "type": "TRADE",
            "account_number": "A1",
            "symbol": "SPY260717P00475000",
            "security_type": "OPTION",
            "side": "SELL",
            "principal_amount": 120,
            "quantity": 1,
            "fees": 0.65,
        }
        fills = normalize_fills(
            [
                {**base, "id": "TX-1", "timestamp": "2026-06-10T14:30:00+00:00"},
                {**base, "id": "TX-2", "timestamp": "2026-06-10T14:31:00+00:00"},
            ]
        )
        self.assertEqual(len(fills), 2)
        self.assertNotEqual(fills[0]["fill_id"], fills[1]["fill_id"])
        self.assertTrue(all(fill["strategy"] == "CSP" for fill in fills))

    def test_public_snapshot_paginates_and_deduplicates_cursor_overlap(self):
        class FakeClient:
            def __init__(self):
                self.requests = []

            def get_history(self, request):
                self.requests.append(request)
                if request.next_token is None:
                    return SimpleNamespace(
                        transactions=[
                            SimpleNamespace(
                                id="OLD",
                                timestamp=datetime(2026, 6, 10, 14, 0, tzinfo=timezone.utc),
                                type="TRADE",
                                sub_type="",
                                account_number="A1",
                                symbol="AAPL",
                                security_type="EQUITY",
                                side="BUY",
                                description="",
                                net_amount=-200,
                                principal_amount=-200,
                                quantity=1,
                                direction="",
                                fees=0,
                            )
                        ],
                        next_token="page-2",
                    )
                return SimpleNamespace(
                    transactions=[
                        SimpleNamespace(
                            id="NEW",
                            timestamp=datetime(2026, 6, 10, 14, 5, tzinfo=timezone.utc),
                            type="TRADE",
                            sub_type="",
                            account_number="A1",
                            symbol="AAPL",
                            security_type="EQUITY",
                            side="BUY",
                            description="",
                            net_amount=-205,
                            principal_amount=-205,
                            quantity=1,
                            direction="",
                            fees=0,
                        )
                    ],
                    next_token=None,
                )

            def get_portfolio(self):
                return SimpleNamespace(
                    positions=[
                        SimpleNamespace(
                            instrument=SimpleNamespace(symbol="AAPL", name="Apple", type="EQUITY"),
                            quantity=2,
                            current_value=410,
                            last_price=SimpleNamespace(last_price=205),
                            percent_of_portfolio=0.1,
                        )
                    ],
                    buying_power=SimpleNamespace(
                        buying_power=1000,
                        cash_only_buying_power=900,
                        options_buying_power=800,
                    ),
                )

        client = FakeClient()
        cursor = {
            "last_timestamp": "2026-06-10T14:00:00+00:00",
            "seen_transaction_ids": ["OLD"],
        }
        snapshot, new_cursor = build_snapshot(
            client,
            lambda **kwargs: SimpleNamespace(**kwargs),
            cursor=cursor,
            overlap_minutes=15,
        )
        self.assertEqual(snapshot["history"]["pages"], 2)
        self.assertEqual(snapshot["history"]["new_transactions"], 1)
        self.assertEqual([fill["transaction_ids"] for fill in snapshot["fills"]], [["NEW"]])
        self.assertEqual(snapshot["positions"][0]["symbol"], "AAPL")
        self.assertEqual(snapshot["options_bp"], 800)
        self.assertEqual(client.requests[0].start.isoformat(), "2026-06-10T13:45:00+00:00")
        self.assertEqual(new_cursor["last_timestamp"], "2026-06-10T14:05:00+00:00")
        self.assertEqual(set(new_cursor["seen_transaction_ids"]), {"OLD", "NEW"})

    def test_operator_commands_feed_public_snapshot_into_storage(self):
        cfg = {
            "journal": {"path": "state/trades.json"},
            "storage": {"path": "state/quant_tools.db", "broker_snapshot": None},
            "public_ingestion": {
                "cursor_path": "state/public_fill_cursor.json",
                "page_size": 50,
                "max_pages": 25,
                "overlap_minutes": 10,
            },
        }
        snapshot = ROOT / "reports" / "run" / "public_broker_snapshot.json"
        ingestion = build_public_ingestion_cmd(cfg, snapshot)
        self.assertIn("public_fill_ingestion.py", ingestion[1])
        self.assertEqual(ingestion[ingestion.index("--output") + 1], str(snapshot))
        self.assertEqual(ingestion[ingestion.index("--page-size") + 1], "50")

        args = Namespace(config=None, journal=None, broker_snapshot=None)
        storage = build_storage_cmd(
            cfg,
            args,
            ROOT / "reports" / "run",
            ROOT / "reports" / "run" / "risk.json",
            ROOT / "reports" / "run" / "tickets.json",
            broker_snapshot_override=snapshot,
        )
        self.assertEqual(storage[storage.index("--broker-snapshot") + 1], str(snapshot))

    def test_osi_parsing(self):
        parts = parse_osi_parts("AAPL260116C00270000")
        self.assertEqual(parts["underlying"], "AAPL")
        self.assertEqual(parts["expiration"], "2026-01-16")
        self.assertEqual(parts["option_type"], "C")
        self.assertEqual(parse_osi_strike("AAPL260116C00270000"), 270.0)
        self.assertEqual(parse_osi_expiration("AAPL260116C00270000"), "2026-01-16")

    def test_scoring_adds_ranked_candidates(self):
        report = score_results(sample_scan_report())
        ranked = report["ranked_candidates"]
        self.assertEqual(len(ranked), 2)
        self.assertGreaterEqual(ranked[0]["score"], ranked[1]["score"])
        self.assertIn("score_components", ranked[0])
        self.assertIn("execution", ranked[0])
        self.assertIn(ranked[0]["verdict"], {"DEPLOY", "SMALL_SIZE", "WATCH", "SKIP"})

    def test_execution_quality_penalizes_wide_spreads(self):
        tight = {
            "strategy": "CSP",
            "credit": 1.0,
            "bid": 0.98,
            "ask": 1.02,
            "volume": 500,
            "open_interest": 1500,
        }
        wide = {
            "strategy": "CSP",
            "credit": 1.0,
            "bid": 0.50,
            "ask": 1.50,
            "volume": 10,
            "open_interest": 50,
        }
        self.assertGreater(execution_quality(tight)["execution_score"], execution_quality(wide)["execution_score"])
        self.assertEqual(execution_quality(tight)["execution_grade"], "A")

    def test_expiration_selection_and_wing_widths(self):
        expirations = [
            (date.today() + timedelta(days=21)).isoformat(),
            (date.today() + timedelta(days=35)).isoformat(),
            (date.today() + timedelta(days=44)).isoformat(),
            (date.today() + timedelta(days=90)).isoformat(),
        ]
        selected = select_expirations(expirations, min_dte=20, max_dte=50, max_expirations=2)
        self.assertEqual(len(selected), 2)
        self.assertEqual(selected[0][1], 35)
        self.assertEqual(parse_wing_widths([5, 2.5, 5, 10]), [2.5, 5.0, 10.0])

    def test_pretrade_rejects_oversized_csp(self):
        report = sample_scan_report()
        portfolio = {
            "portfolio": {"positions": [{"symbol": "SPY", "current_value": 5000}]},
            "risk": {"net_delta_shares": 80},
        }
        result = evaluate_report(report, portfolio, RiskLimits(account_nav=30000))
        decisions = {row["strategy"]: row for row in result["decisions"]}
        self.assertEqual(decisions["BULL_PUT"]["risk_decision"], "APPROVE")
        self.assertEqual(decisions["CSP"]["risk_decision"], "REJECT")

    def test_trade_journal_pnl_and_stats(self):
        state = default_state()
        add_args = Namespace(
            id="TEST-001",
            ticker="SPY",
            strategy="BULL_PUT",
            opened_at="2026-06-01",
            quantity=1,
            entry_credit=1.20,
            entry_debit=0.0,
            capital_at_risk=380.0,
            max_loss=380.0,
            score=66.0,
            verdict="WATCH",
            pop_pct=78.0,
            ann_roc_pct=20.4,
            dte=43,
            expiration="2026-07-17",
            strikes="475/470",
            thesis="defined risk",
            tags="test,spread",
        )
        add_trade(state, add_args)
        close_args = Namespace(
            id="TEST-001",
            exit_credit=0.0,
            exit_debit=0.45,
            closed_at="2026-06-04",
            note="closed",
        )
        trade = close_trade(state, close_args)
        self.assertEqual(trade["realized_pnl"], 75.0)
        stats = journal_stats(state["trades"])
        self.assertEqual(stats["closed_trades"], 1)
        self.assertEqual(stats["total_realized_pnl"], 75.0)

    def test_incomplete_open_trade_creates_high_priority_alert(self):
        report = build_alerts(
            None,
            {
                "trades": [
                    {
                        "id": "T-INCOMPLETE",
                        "status": "OPEN",
                        "ticker": "NVDA",
                        "strategy": "CSP",
                    }
                ]
            },
            68,
            50,
            21,
        )
        self.assertEqual(report["summary"]["high"], 1)
        self.assertEqual(report["alerts"][0]["kind"], "incomplete_journal")

    def test_performance_profiles_by_ticker_strategy(self):
        trades = [
            {"status": "CLOSED", "ticker": "SPY", "strategy": "BULL_PUT", "realized_pnl": -50},
            {"status": "CLOSED", "ticker": "SPY", "strategy": "BULL_PUT", "realized_pnl": -25},
            {"status": "CLOSED", "ticker": "SPY", "strategy": "BULL_PUT", "realized_pnl": 10},
        ]
        profiles = build_profiles(trades)
        scope, profile = lookup_profile(profiles, "SPY", "BULL_PUT")
        self.assertEqual(scope, "ticker_strategy")
        self.assertEqual(profile["signal"], "THROTTLE")

    def test_action_plan_throttles_weak_profile(self):
        trades = [
            {"status": "CLOSED", "ticker": "SPY", "strategy": "BULL_PUT", "realized_pnl": -50},
            {"status": "CLOSED", "ticker": "SPY", "strategy": "BULL_PUT", "realized_pnl": -25},
            {"status": "CLOSED", "ticker": "SPY", "strategy": "BULL_PUT", "realized_pnl": 10},
        ]
        portfolio = {
            "portfolio": {"positions": [{"symbol": "SPY", "current_value": 5000}]},
            "risk": {"net_delta_shares": 80},
        }
        plan = build_action_plan(sample_scan_report(), portfolio, {"trades": trades}, RiskLimits(account_nav=30000))
        bull_put = next(row for row in plan["actions"] if row["strategy"] == "BULL_PUT")
        self.assertEqual(bull_put["profile_signal"], "THROTTLE")
        self.assertEqual(bull_put["action_decision"], "REDUCE")

    def test_alerts_from_plan_and_journal(self):
        plan = {
            "actions": [
                {
                    "action_decision": "APPROVE",
                    "score": 72,
                    "ticker": "SPY",
                    "strategy": "BULL_PUT",
                    "action_size_multiplier": 1.0,
                    "candidate": {
                        "execution": {
                            "suggested_limit_credit": 1.1,
                            "do_not_chase_below": 1.0,
                            "execution_grade": "B",
                        }
                    },
                }
            ]
        }
        journal = {
            "trades": [
                {
                    "id": "OPEN-1",
                    "status": "OPEN",
                    "ticker": "SPY",
                    "strategy": "BULL_PUT",
                    "unrealized_pnl_pct": 55,
                    "expiration": (date.today() + timedelta(days=5)).isoformat(),
                    "strikes": "475/470",
                }
            ]
        }
        report = build_alerts(plan, journal, min_score=68, profit_target_pct=50, dte_warning=21)
        kinds = {row["kind"] for row in report["alerts"]}
        self.assertEqual(report["summary"]["high"], 3)
        self.assertIn("candidate", kinds)
        self.assertIn("profit_target", kinds)
        self.assertIn("dte_warning", kinds)

    def test_alerts_include_validation_and_drift_health(self):
        report = build_alerts(
            None,
            None,
            min_score=68,
            profit_target_pct=50,
            dte_warning=21,
            validation={
                "summary": {
                    "status": "WATCH",
                    "profitable_fold_pct": 40,
                    "avg_oos_expectancy": -10,
                }
            },
            drift={
                "summary": {"status": "DRIFT", "severity": "HIGH"},
                "comparison": {"expectancy_change": -75, "win_rate_change": -25},
            },
        )
        kinds = {row["kind"] for row in report["alerts"]}
        self.assertEqual(report["summary"]["high"], 1)
        self.assertIn("validation", kinds)
        self.assertIn("drift", kinds)

    def test_alerts_include_execution_and_position_exceptions(self):
        report = build_alerts(
            None,
            None,
            min_score=68,
            profit_target_pct=50,
            dte_warning=21,
            reconciliation={
                "summary": {
                    "overfilled_tickets": 1,
                    "stale_partial_tickets": 1,
                    "duplicate_active_setups": 1,
                    "unmatched_fills": 1,
                    "unknown_effect_fills": 1,
                    "matched_exit_fills": 1,
                },
                "lifecycle_events": [
                    {
                        "event_type": "ASSIGNMENT",
                        "ticker": "SPY",
                        "description": "Option assigned",
                    }
                ],
            },
        )
        kinds = {row["kind"] for row in report["alerts"]}
        self.assertIn("execution_exception", kinds)
        self.assertIn("broker_exit", kinds)
        self.assertIn("position_event", kinds)
        self.assertGreaterEqual(report["summary"]["high"], 3)

    def test_correlation_penalty_for_group_overlap(self):
        portfolio = {"portfolio": {"positions": [{"symbol": "AAPL", "current_value": 12000}]}}
        groups = {"mega_cap_tech": ["AAPL", "MSFT", "NVDA"]}
        penalty = correlation_penalty("NVDA", portfolio, groups, account_nav=30000, warning_pct=0.25)
        self.assertGreater(penalty["penalty"], 0)
        self.assertEqual(penalty["dominant_group"], "mega_cap_tech")

    def test_execution_ticket_builder(self):
        plan = {
            "actions": [
                {
                    "action_decision": "APPROVE",
                    "ticker": "SPY",
                    "strategy": "BULL_PUT",
                    "action_size_multiplier": 1.0,
                    "score": 72,
                    "max_loss": 380,
                    "capital_required": 380,
                    "checks": [],
                    "candidate": {
                        "strategy": "BULL_PUT",
                        "expiration": "2026-07-17",
                        "dte": 43,
                        "pop_pct": 78,
                        "ann_roc_pct": 20.4,
                        "short_strike": 475,
                        "long_strike": 470,
                        "execution": {
                            "suggested_limit_credit": 1.1,
                            "do_not_chase_below": 1.0,
                            "execution_grade": "B",
                        },
                    },
                    "portfolio_allocation": {
                        "rank": 1,
                        "objective_score": 75,
                        "tail_loss": 247,
                    },
                }
            ]
        }
        tickets = build_tickets(plan)
        self.assertEqual(len(tickets), 1)
        self.assertEqual(tickets[0]["order_action"], "SELL_SPREAD_TO_OPEN")
        self.assertEqual(tickets[0]["target_quantity"], 1.0)
        self.assertEqual(tickets[0]["strikes"], "475/470")
        self.assertEqual(tickets[0]["portfolio_allocation"]["rank"], 1)
        self.assertEqual(tickets[0]["pop_pct"], 78)
        self.assertEqual(tickets[0]["ann_roc_pct"], 20.4)
        self.assertTrue(tickets[0]["ticket_id"].startswith("QTK-"))

    def test_ticket_ids_are_distinct_across_plan_issuances(self):
        action = {
            "action_decision": "APPROVE",
            "ticker": "SPY",
            "strategy": "BULL_PUT",
            "score": 72,
            "candidate": {
                "strategy": "BULL_PUT",
                "expiration": "2026-07-17",
                "short_strike": 475,
                "long_strike": 470,
                "execution": {},
            },
        }
        first = build_tickets({"created_at": "2026-06-10T13:00:00+00:00", "actions": [action]})[0]
        repeated = build_tickets({"created_at": "2026-06-10T13:00:00+00:00", "actions": [action]})[0]
        later = build_tickets({"created_at": "2026-06-11T13:00:00+00:00", "actions": [action]})[0]
        self.assertEqual(first["ticket_id"], repeated["ticket_id"])
        self.assertNotEqual(first["ticket_id"], later["ticket_id"])
        self.assertEqual(first["lifecycle_status"], "READY")
        self.assertTrue(first["issued_at"])
        self.assertTrue(first["execution_batch_id"])
        self.assertTrue(first["expires_at"])

    def test_ticket_generation_suppresses_equivalent_active_setup(self):
        action = {
            "action_decision": "APPROVE",
            "ticker": "SPY",
            "strategy": "BULL_PUT",
            "score": 72,
            "candidate": {
                "strategy": "BULL_PUT",
                "expiration": "2026-07-17",
                "short_strike": 475,
                "long_strike": 470,
                "execution": {},
            },
        }
        report = build_ticket_report(
            {"created_at": "2026-06-10T13:00:00+00:00", "actions": [action]},
            active_tickets=[
                {
                    "ticket_id": "QTK-ACTIVE",
                    "ticker": "SPY",
                    "strategy": "BULL_PUT",
                    "expiration": "2026-07-17",
                    "strikes": "475/470",
                    "quantity": 1,
                    "entry_credit": 1.2,
                    "capital_at_risk": 380,
                }
            ],
        )
        self.assertEqual(report["tickets"], [])
        self.assertEqual(report["suppressed_duplicates"][0]["active_ticket_ids"], ["QTK-ACTIVE"])

    def test_historical_analytics_tracks_expectancy_and_drawdown(self):
        state = {
            "trades": [
                {"id": "1", "status": "CLOSED", "closed_at": "2026-06-01", "ticker": "SPY", "strategy": "BULL_PUT", "score": 72, "realized_pnl": 100, "capital_at_risk": 500},
                {"id": "2", "status": "CLOSED", "closed_at": "2026-06-02", "ticker": "SPY", "strategy": "BULL_PUT", "score": 65, "realized_pnl": -150, "capital_at_risk": 500},
                {"id": "3", "status": "CLOSED", "closed_at": "2026-06-03", "ticker": "QQQ", "strategy": "CSP", "score": 75, "realized_pnl": 50, "capital_at_risk": 1000},
            ]
        }
        report = build_analytics(state)
        self.assertEqual(report["overall"]["count"], 3)
        self.assertEqual(report["overall"]["expectancy"], 0.0)
        self.assertEqual(report["drawdown"]["max_drawdown"], 150.0)
        self.assertEqual(len(report["equity_curve"]), 3)

    def test_adaptive_sizing_throttles_negative_realized_edge(self):
        state = {
            "trades": [
                {
                    "id": str(i),
                    "status": "CLOSED",
                    "closed_at": f"2026-06-{i + 1:02d}",
                    "ticker": "SPY",
                    "strategy": "BULL_PUT",
                    "score": 65,
                    "realized_pnl": -50,
                    "capital_at_risk": 500,
                }
                for i in range(5)
            ]
        }
        sizing = adaptive_size("SPY", "BULL_PUT", build_analytics(state), {"min_trades": 5})
        self.assertLess(sizing["multiplier"], 0.75)
        self.assertEqual(sizing["scope"], "ticker_strategy")

    def test_feedback_calibration_raises_floor_above_bad_band(self):
        trades = []
        for i in range(5):
            trades.append({"id": f"L{i}", "status": "CLOSED", "closed_at": f"2026-05-{i + 1:02d}", "strategy": "CSP", "ticker": "SPY", "score": 65, "realized_pnl": -20, "capital_at_risk": 500})
            trades.append({"id": f"H{i}", "status": "CLOSED", "closed_at": f"2026-06-{i + 1:02d}", "strategy": "CSP", "ticker": "QQQ", "score": 75, "realized_pnl": 40, "capital_at_risk": 500})
        feedback = build_feedback_report({"trades": trades}, current_min_score=55, min_samples=5)
        self.assertEqual(feedback["recommended_min_score"], 70.0)
        self.assertEqual(feedback["strategy_adjustments"]["CSP"]["signal"], "BOOST")

    def test_operator_summary_is_send_ready(self):
        text = build_summary(
            {"summary": {"approve": 1, "reduce": 0, "reject": 0}, "actions": []},
            {"summary": {"high": 0}},
            {"tickets": []},
            {"overall": {"count": 3, "win_rate": 66.7, "expectancy": 25, "total_pnl": 75}, "drawdown": {"max_drawdown": 20}},
            {"recommended_min_score": 60},
            management={
                "summary": {"open_trades": 1, "high_urgency": 1, "strike_threats": 1},
                "actions": [
                    {
                        "urgency": "HIGH",
                        "ticker": "SPY",
                        "strategy": "CSP",
                        "action": "ROLL_OR_CLOSE",
                        "dte": 12,
                        "reasons": ["STRIKE_THREAT"],
                        "strike_threat": {
                            "status": "THREAT",
                            "sigma_distance": 0.4,
                            "option_type": "P",
                            "short_strike": 475,
                        },
                    }
                ],
            },
        )
        self.assertIn("Quant Tools Morning Review", text)
        self.assertIn("Recommended minimum score: 60.0", text)
        self.assertIn("Do not place orders without explicit confirmation", text)
        self.assertIn("Open Position Management", text)
        self.assertIn("ROLL_OR_CLOSE", text)

    def test_sqlite_storage_migrates_and_upserts(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "quant.db"
            con = connect(db_path)
            try:
                trade = {"id": "T1", "ticket_id": "QTK-1", "status": "OPEN", "ticker": "SPY", "strategy": "BULL_PUT"}
                ticket = {"ticket_id": "QTK-1", "ticker": "SPY", "strategy": "BULL_PUT", "decision": "APPROVE"}
                upsert_trades(con, [trade])
                upsert_trades(con, [{**trade, "status": "CLOSED"}])
                upsert_tickets(con, [ticket])
                upsert_equity_lots(
                    con,
                    [
                        {
                            "id": "LOT-A1",
                            "ticker": "SPY",
                            "status": "OPEN",
                            "quantity": 100,
                            "cost_basis_per_share": 472.5,
                            "assignment_event_id": "A1",
                        }
                    ],
                )
                counts = table_counts(con)
                self.assertEqual(counts["trades"], 1)
                self.assertEqual(counts["tickets"], 1)
                self.assertEqual(export_journal_state(con)["trades"][0]["status"], "CLOSED")
                self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], 6)
                self.assertEqual(counts["option_chain_snapshots"], 0)
                self.assertEqual(counts["equity_lots"], 1)
                self.assertEqual(export_journal_state(con)["equity_lots"][0]["quantity"], 100)
            finally:
                con.close()

    def test_sqlite_migrates_existing_version_one_ticket_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "legacy.db"
            legacy = sqlite3.connect(db_path)
            try:
                legacy.executescript(
                    """
                    CREATE TABLE tickets (
                        ticket_id TEXT PRIMARY KEY,
                        ticker TEXT,
                        strategy TEXT,
                        decision TEXT,
                        expiration TEXT,
                        payload_json TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    INSERT INTO tickets VALUES (
                        'QTK-LEGACY', 'SPY', 'CSP', 'APPROVE', '2026-07-17',
                        '{"ticket_id":"QTK-LEGACY","ticker":"SPY","strategy":"CSP"}',
                        '2026-06-10T12:00:00'
                    );
                    PRAGMA user_version = 1;
                    """
                )
                legacy.commit()
            finally:
                legacy.close()

            con = connect(db_path)
            try:
                self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], 6)
                self.assertEqual(load_active_tickets(con), [])
                ticket = list_tickets(con, ["EXPIRED"])[0]
                self.assertEqual(ticket["ticket_id"], "QTK-LEGACY")
                self.assertEqual(ticket["lifecycle_status"], "EXPIRED")
                self.assertEqual(ticket["target_quantity"], 1.0)
            finally:
                con.close()

    def test_option_chain_snapshots_are_queryable(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "quant.db")
            try:
                inserted = insert_option_chain_snapshot(
                    con,
                    captured_at="2026-06-11T10:30:00-04:00",
                    ticker="SPY",
                    expiration="2026-07-17",
                    spot=600,
                    dte=36,
                    chain={
                        "calls": {
                            610.0: {
                                "osi": "SPY260717C00610000",
                                "bid": 4.1,
                                "ask": 4.3,
                                "mark": 4.2,
                                "delta": 0.3,
                                "theta": -0.12,
                            }
                        },
                        "puts": {
                            590.0: {
                                "osi": "SPY260717P00590000",
                                "bid": 3.8,
                                "ask": 4.0,
                                "mark": 3.9,
                                "delta": -0.28,
                                "theta": -0.11,
                            }
                        },
                    },
                )
                self.assertEqual(inserted, 2)
                self.assertEqual(table_counts(con)["option_chain_snapshots"], 2)
                row = con.execute(
                    "SELECT osi, delta FROM option_chain_snapshots WHERE option_type='PUT'"
                ).fetchone()
                self.assertEqual(row["osi"], "SPY260717P00590000")
                self.assertEqual(row["delta"], -0.28)
            finally:
                con.close()

    def test_scorecard_reports_pop_calibration_and_monthly_excess_return(self):
        report = build_scorecard(
            {
                "trades": [
                    {
                        "id": "T1",
                        "status": "CLOSED",
                        "closed_at": "2026-05-10",
                        "pop_pct": 70,
                        "quantity": 1,
                        "entry_credit": 1.0,
                        "realized_pnl": 80,
                    },
                    {
                        "id": "T2",
                        "status": "CLOSED",
                        "closed_at": "2026-05-20",
                        "pop_pct": 72,
                        "quantity": 1,
                        "entry_credit": 1.0,
                        "realized_pnl": -40,
                    },
                ]
            },
            account_nav=10000,
            spy_returns={"2026-05": 1.0},
        )
        bucket = report["pop_calibration"]["buckets"]["65-74"]
        self.assertEqual(bucket["count"], 2)
        self.assertEqual(bucket["expected_pop_pct"], 71.0)
        self.assertEqual(bucket["realized_win_rate_pct"], 50.0)
        month = report["monthly"]["2026-05"]
        self.assertEqual(month["realized_pnl"], 40.0)
        self.assertEqual(month["account_return_pct"], 0.4)
        self.assertEqual(month["excess_return_vs_spy_pct"], -0.6)

    def test_ticket_lifecycle_completes_across_separate_sync_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "quant.db"
            ticket = {
                "ticket_id": "QTK-LIFECYCLE",
                "issued_at": "2026-06-10T13:00:00+00:00",
                "lifecycle_status": "PENDING",
                "ticker": "SPY",
                "strategy": "CSP",
                "expiration": "2026-07-17",
                "strikes": "475",
                "target_quantity": 2,
                "limit_credit": 1.20,
            }

            con = connect(db_path)
            try:
                upsert_tickets(con, [ticket])
                upsert_fills(
                    con,
                    [
                        {
                            "fill_id": "F-DAY-1",
                            "ticker": "SPY",
                            "strategy": "CSP",
                            "expiration": "2026-07-17",
                            "strikes": "475",
                            "quantity": 1,
                            "net_credit": 1.18,
                            "filled_at": "2026-06-10T14:00:00+00:00",
                        }
                    ],
                )
                active = load_active_tickets(con)
                fills = load_fills_for_reconciliation(con, ["QTK-LIFECYCLE"], ["F-DAY-1"])
                first_report = build_reconciliation(
                    {"trades": []},
                    {"tickets": active},
                    {"positions": [], "fills": fills},
                )
                apply_ticket_lifecycle(con, first_report["ticket_matches"])
                self.assertEqual(ticket_lifecycle_counts(con), {"PARTIAL": 1})
            finally:
                con.close()

            con = connect(db_path)
            try:
                upsert_fills(
                    con,
                    [
                        {
                            "fill_id": "F-DAY-2",
                            "ticker": "SPY",
                            "strategy": "CSP",
                            "expiration": "2026-07-17",
                            "strikes": "475",
                            "quantity": 1,
                            "net_credit": 1.22,
                            "filled_at": "2026-06-11T14:00:00+00:00",
                        }
                    ],
                )
                active = load_active_tickets(con)
                self.assertEqual([row["ticket_id"] for row in active], ["QTK-LIFECYCLE"])
                fills = load_fills_for_reconciliation(con, ["QTK-LIFECYCLE"], ["F-DAY-2"])
                second_report = build_reconciliation(
                    {"trades": []},
                    {"tickets": active},
                    {"positions": [], "fills": fills},
                )
                match = second_report["ticket_matches"][0]
                self.assertEqual(match["status"], "MATCHED")
                self.assertEqual(match["fill_ids"], ["F-DAY-1", "F-DAY-2"])
                self.assertEqual(match["fill_price"], 1.2)
                apply_ticket_lifecycle(con, second_report["ticket_matches"])
                self.assertEqual(ticket_lifecycle_counts(con), {"FILLED": 1})
                self.assertEqual(load_active_tickets(con), [])
            finally:
                con.close()

    def test_storage_sync_reconciles_outstanding_ticket_without_ticket_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "quant.db"
            ticket = {
                "ticket_id": "QTK-SYNC",
                "issued_at": "2026-06-10T13:00:00+00:00",
                "ticker": "SPY",
                "strategy": "CSP",
                "expiration": "2026-07-17",
                "strikes": "475",
                "target_quantity": 2,
                "limit_credit": 1.20,
            }
            first = sync_artifacts(
                db_path,
                {"trades": []},
                [ticket],
                [],
                [
                    {
                        "fill_id": "F-SYNC-1",
                        "ticker": "SPY",
                        "strategy": "CSP",
                        "expiration": "2026-07-17",
                        "strikes": "475",
                        "quantity": 1,
                        "net_credit": 1.18,
                    }
                ],
            )
            self.assertEqual(first["ticket_lifecycle"], {"PARTIAL": 1})

            second = sync_artifacts(
                db_path,
                {"trades": []},
                [],
                [],
                [
                    {
                        "fill_id": "F-SYNC-2",
                        "ticker": "SPY",
                        "strategy": "CSP",
                        "expiration": "2026-07-17",
                        "strikes": "475",
                        "quantity": 1,
                        "net_credit": 1.22,
                    }
                ],
            )
            self.assertEqual(second["ticket_lifecycle"], {"FILLED": 1})
            match = second["reconciliation"]["ticket_matches"][0]
            self.assertEqual(match["fill_ids"], ["F-SYNC-1", "F-SYNC-2"])
            self.assertEqual(match["fill_price"], 1.2)

    def test_new_fill_completes_old_pending_ticket_before_expiry_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = sync_artifacts(
                Path(tmp) / "quant.db",
                {"trades": []},
                [
                    {
                        "ticket_id": "QTK-OLD-FILL",
                        "issued_at": "2026-06-01T12:00:00+00:00",
                        "ticker": "SPY",
                        "strategy": "CSP",
                        "expiration": "2026-07-17",
                        "strikes": "475",
                        "target_quantity": 1,
                    }
                ],
                [],
                [
                    {
                        "fill_id": "F-OLD-FILL",
                        "ticker": "SPY",
                        "strategy": "CSP",
                        "expiration": "2026-07-17",
                        "strikes": "475",
                        "quantity": 1,
                        "net_credit": 1.2,
                    }
                ],
                pending_expiry_hours=1,
            )
            self.assertEqual(result["ticket_lifecycle"], {"FILLED": 1})
            self.assertEqual(result["reconciliation"]["summary"]["expired_tickets"], 0)

    def test_ticket_lifecycle_can_be_cancelled_and_reopened(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "quant.db")
            try:
                upsert_tickets(
                    con,
                    [
                        {
                            "ticket_id": "QTK-CANCEL",
                            "ticker": "SPY",
                            "strategy": "CSP",
                            "target_quantity": 1,
                        }
                    ],
                )
                self.assertTrue(set_ticket_lifecycle(con, "QTK-CANCEL", "CANCELLED"))
                self.assertEqual(list_tickets(con, ["CANCELLED"])[0]["lifecycle_status"], "CANCELLED")
                self.assertEqual(load_active_tickets(con), [])
                self.assertTrue(set_ticket_lifecycle(con, "QTK-CANCEL", "PENDING"))
                self.assertEqual(load_active_tickets(con)[0]["ticket_id"], "QTK-CANCEL")
            finally:
                con.close()

    def test_lifecycle_policy_expires_pending_but_preserves_stale_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "quant.db")
            try:
                upsert_tickets(
                    con,
                    [
                        {
                            "ticket_id": "QTK-STALE-PENDING",
                            "issued_at": "2026-06-08T12:00:00+00:00",
                            "ticker": "SPY",
                            "strategy": "CSP",
                            "expiration": "2026-07-17",
                            "strikes": "475",
                        },
                        {
                            "ticket_id": "QTK-STALE-PARTIAL",
                            "issued_at": "2026-06-09T12:00:00+00:00",
                            "lifecycle_status": "PARTIAL",
                            "filled_quantity": 1,
                            "target_quantity": 2,
                            "ticker": "QQQ",
                            "strategy": "CSP",
                            "expiration": "2026-07-17",
                            "strikes": "450",
                        },
                    ],
                )
                con.execute(
                    "UPDATE tickets SET lifecycle_status='PARTIAL', filled_quantity=1 WHERE ticket_id='QTK-STALE-PARTIAL'"
                )
                con.commit()
                policy = apply_lifecycle_policy(
                    con,
                    pending_expiry_hours=24,
                    partial_review_hours=4,
                    now=datetime(2026, 6, 10, 16, 0, tzinfo=timezone.utc),
                )
                self.assertEqual(
                    [row["ticket_id"] for row in policy["expired_tickets"]],
                    ["QTK-STALE-PENDING"],
                )
                self.assertEqual(
                    [row["ticket_id"] for row in policy["stale_partial_tickets"]],
                    ["QTK-STALE-PARTIAL"],
                )
                self.assertEqual(ticket_lifecycle_counts(con), {"EXPIRED": 1, "PARTIAL": 1})
            finally:
                con.close()

    def test_lifecycle_policy_reports_duplicate_active_setups(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "quant.db")
            try:
                base = {
                    "issued_at": "2026-06-10T12:00:00+00:00",
                    "ticker": "SPY",
                    "strategy": "BULL_PUT",
                    "expiration": "2026-07-17",
                    "strikes": "475/470",
                    "quantity": 1,
                    "entry_credit": 1.2,
                    "capital_at_risk": 380,
                }
                upsert_tickets(
                    con,
                    [
                        {**base, "ticket_id": "QTK-DUP-1"},
                        {**base, "ticket_id": "QTK-DUP-2"},
                    ],
                )
                policy = apply_lifecycle_policy(
                    con,
                    pending_expiry_hours=24,
                    partial_review_hours=4,
                    now=datetime(2026, 6, 10, 13, 0, tzinfo=timezone.utc),
                )
                duplicate = policy["duplicate_active_setups"][0]
                self.assertEqual(duplicate["count"], 2)
                self.assertEqual(duplicate["ticket_ids"], ["QTK-DUP-1", "QTK-DUP-2"])
                self.assertEqual(ticket_lifecycle_counts(con), {"READY": 2})
            finally:
                con.close()

    def test_database_maintenance_creates_backup_and_prunes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "quant.db"
            backup_dir = Path(tmp) / "backups"
            con = connect(db_path)
            try:
                upsert_trades(con, [{"id": "T1", "status": "OPEN", "ticker": "SPY", "strategy": "CSP"}])
            finally:
                con.close()
            report = maintain_database(db_path, backup_dir, retention_days=0, keep_last=1)
            self.assertTrue(report["ok"])
            self.assertTrue(Path(report["backup"]).exists())
            older = backup_database(db_path, backup_dir)
            self.assertTrue(older.exists())
            deleted = prune_backups(backup_dir, retention_days=0, keep_last=1)
            self.assertEqual(len(deleted), 1)

    def test_health_report_passes_without_live_database(self):
        report = build_health_report(include_tests=False, include_db=True, db_path=Path("missing-health-test.db"))
        self.assertTrue(report["ok"])
        self.assertTrue(any(check["name"] == "database_integrity" for check in report["checks"]))

    def test_scenario_stress_estimates_equity_loss(self):
        report = build_scenario_report(
            {
                "portfolio": {
                    "positions": [
                        {"symbol": "SPY", "type": "EQUITY", "quantity": 100, "current_value": 50000}
                    ]
                },
                "risk": {"total_value": 50000, "portfolio_beta": 1.0},
            },
            [{"name": "down_10", "market_shock_pct": -10, "vol_shock_pct": 0}],
        )
        self.assertEqual(report["summary"]["worst_scenario"], "down_10")
        self.assertEqual(report["summary"]["worst_pnl"], -5000.0)
        self.assertEqual(report["summary"]["worst_pnl_pct_nav"], -10.0)

    def test_scenario_stress_uses_option_greeks_with_underlying_spot(self):
        report = build_scenario_report(
            {
                "portfolio": {
                    "positions": [
                        {
                            "symbol": "SPY260717P00475000",
                            "type": "OPTION",
                            "quantity": -1,
                            "current_value": -200,
                            "underlying_price": 500,
                            "greeks": {"delta": -0.2, "gamma": 0.001, "vega": 0.1},
                        }
                    ]
                },
                "risk": {"total_value": 10000, "portfolio_beta": 1.0},
            },
            [{"name": "down_10_vol_up", "market_shock_pct": -10, "vol_shock_pct": 10}],
        )
        worst = report["scenarios"][0]["worst_positions"][0]
        self.assertEqual(worst["model"], "greeks")
        self.assertEqual(report["summary"]["worst_pnl"], -1225.0)

    def test_portfolio_allocator_selects_best_risk_budgeted_basket(self):
        def action(ticker, score, capital, max_loss, delta, ann_roc):
            return {
                "ticker": ticker,
                "strategy": "BULL_PUT",
                "score": score,
                "action_decision": "APPROVE",
                "action_size_multiplier": 1.0,
                "capital_required": capital,
                "max_loss": max_loss,
                "delta_change": delta,
                "projected_delta": delta,
                "correlation": {"groups": [], "penalty": 0},
                "candidate": {
                    "strategy": "BULL_PUT",
                    "expiration": "2026-07-17",
                    "short_strike": 100,
                    "long_strike": 95,
                    "pop_pct": 75,
                    "ann_roc_pct": ann_roc,
                    "execution": {"execution_score": 80},
                },
            }

        report = allocate_portfolio(
            {
                "limits": {"account_nav": 10000, "max_portfolio_delta_abs": 100},
                "actions": [
                    action("AAA", 80, 1000, 1000, -20, 30),
                    action("BBB", 70, 1000, 1000, -20, 20),
                    action("CCC", 60, 1000, 1000, -20, 10),
                ],
            },
            {
                "max_positions": 2,
                "max_total_capital_pct": 0.25,
                "max_tail_loss_pct": 0.20,
                "max_ticker_capital_pct": 0.20,
                "max_group_exposure_pct": 0.50,
                "stress_loss_fraction": 0.50,
            },
        )
        self.assertEqual([row["ticker"] for row in report["selected"]], ["AAA", "BBB"])
        self.assertEqual(report["summary"]["selected"], 2)
        self.assertEqual(report["summary"]["capital_allocated"], 2000.0)
        self.assertIn("position limit 2", report["excluded"][0]["reasons"])

    def test_portfolio_allocator_enforces_tail_loss_budget(self):
        report = allocate_portfolio(
            {
                "limits": {"account_nav": 10000, "max_portfolio_delta_abs": 100},
                "actions": [
                    {
                        "ticker": "SPY",
                        "strategy": "BULL_PUT",
                        "score": 80,
                        "action_decision": "APPROVE",
                        "action_size_multiplier": 1.0,
                        "capital_required": 1200,
                        "max_loss": 1200,
                        "delta_change": -20,
                        "projected_delta": -20,
                        "correlation": {"groups": [], "penalty": 0},
                        "candidate": {
                            "strategy": "BULL_PUT",
                            "expiration": "2026-07-17",
                            "short_strike": 475,
                            "long_strike": 470,
                            "pop_pct": 75,
                            "ann_roc_pct": 20,
                            "execution": {"execution_score": 80},
                        },
                    }
                ],
            },
            {
                "max_tail_loss_pct": 0.05,
                "stress_loss_fraction": 0.65,
                "max_total_capital_pct": 0.50,
                "max_ticker_capital_pct": 0.50,
            },
        )
        self.assertEqual(report["summary"]["selected"], 0)
        self.assertIn("tail-loss budget $500", report["excluded"][0]["reasons"])

    def test_portfolio_allocator_caps_projected_bootstrap_expected_shortfall(self):
        action = {
            "ticker": "SPY",
            "strategy": "BULL_PUT",
            "score": 80,
            "action_decision": "APPROVE",
            "action_size_multiplier": 1.0,
            "capital_required": 1000,
            "max_loss": 1000,
            "delta_change": -20,
            "projected_delta": -20,
            "correlation": {"groups": [], "penalty": 0},
            "candidate": {
                "strategy": "BULL_PUT",
                "expiration": "2026-07-17",
                "short_strike": 475,
                "long_strike": 470,
                "pop_pct": 75,
                "ann_roc_pct": 20,
                "execution": {"execution_score": 80},
            },
        }
        report = allocate_portfolio(
            {
                "limits": {"account_nav": 10000, "max_portfolio_delta_abs": 100},
                "actions": [action],
            },
            {
                "max_expected_shortfall_pct": 0.08,
                "stress_loss_fraction": 0.65,
                "max_total_capital_pct": 0.50,
                "max_ticker_capital_pct": 0.50,
            },
            {
                "risk": {
                    "var_bootstrap": {
                        "expected_shortfall_95": 500,
                        "observations": 250,
                    }
                }
            },
        )
        self.assertEqual(report["limits"]["risk_model"], "bootstrap_expected_shortfall")
        self.assertEqual(report["summary"]["selected"], 0)
        self.assertIn("expected-shortfall budget $800", report["excluded"][0]["reasons"])

    def test_walk_forward_validation_finds_stable_profitable_threshold(self):
        trades = []
        for i in range(20):
            high_score = i % 2 == 0
            trades.append(
                {
                    "id": str(i),
                    "status": "CLOSED",
                    "closed_at": f"2026-05-{i + 1:02d}",
                    "ticker": "SPY",
                    "strategy": "BULL_PUT",
                    "score": 72 if high_score else 52,
                    "realized_pnl": 100 if high_score else -80,
                    "capital_at_risk": 500,
                }
            )
        report = build_walk_forward_report(
            {"trades": trades},
            min_train=10,
            test_window=5,
            thresholds=[50, 60, 70],
            min_selected=3,
        )
        self.assertEqual(report["summary"]["status"], "PASS")
        self.assertEqual(report["summary"]["avg_selected_threshold"], 60.0)
        self.assertEqual(report["summary"]["profitable_fold_pct"], 100.0)

    def test_drift_monitor_flags_recent_edge_deterioration(self):
        trades = []
        for i in range(20):
            recent = i >= 15
            trades.append(
                {
                    "id": str(i),
                    "status": "CLOSED",
                    "closed_at": f"2026-05-{i + 1:02d}",
                    "ticker": "SPY",
                    "strategy": "BULL_PUT",
                    "score": 70,
                    "realized_pnl": -100 if recent else 75,
                    "capital_at_risk": 500,
                }
            )
        report = build_drift_report(
            {"trades": trades},
            recent_window=5,
            min_baseline=10,
            min_samples=5,
        )
        self.assertEqual(report["summary"]["status"], "DRIFT")
        self.assertEqual(report["summary"]["severity"], "HIGH")
        self.assertLess(report["comparison"]["expectancy_change"], 0)

    def test_broker_reconciliation_matches_ticket_and_position(self):
        journal = {
            "trades": [
                {"id": "T1", "ticket_id": "QTK-1", "status": "OPEN", "ticker": "SPY", "strategy": "BULL_PUT"}
            ]
        }
        tickets = {
            "tickets": [
                {"ticket_id": "QTK-1", "ticker": "SPY", "strategy": "BULL_PUT", "limit_credit": 1.1}
            ]
        }
        broker = {
            "fills": [{"fill_id": "F1", "ticket_id": "QTK-1", "ticker": "SPY", "strategy": "BULL_PUT", "price": 1.12}],
            "positions": [{"symbol": "SPY260717P00475000", "type": "OPTION", "quantity": -1}],
        }
        report = build_reconciliation(journal, tickets, broker)
        self.assertEqual(report["summary"]["matched_tickets"], 1)
        self.assertEqual(report["summary"]["missing_positions"], 0)
        self.assertEqual(report["proposed_journal_updates"][0]["set"]["broker_fill_id"], "F1")

        partial = build_reconciliation(
            {
                "trades": [
                    {
                        "id": "T2",
                        "status": "OPEN",
                        "ticker": "SPY",
                        "strategy": "BULL_PUT",
                        "expiration": "2026-07-17",
                        "strikes": "475/470",
                    }
                ]
            },
            {"tickets": []},
            {"positions": [{"symbol": "SPY260717P00475000", "type": "OPTION", "quantity": -1}], "fills": []},
        )
        self.assertEqual(partial["summary"]["partial_positions"], 1)
        self.assertEqual(partial["summary"]["position_exceptions"], 1)

    def test_reconciliation_promotes_staged_trade_when_entry_fills(self):
        report = build_reconciliation(
            {
                "trades": [
                    {
                        "id": "T-STAGED",
                        "ticket_id": "QTK-STAGED",
                        "status": "STAGED",
                        "ticker": "SPY",
                        "strategy": "CSP",
                    }
                ]
            },
            {
                "tickets": [
                    {
                        "ticket_id": "QTK-STAGED",
                        "ticker": "SPY",
                        "strategy": "CSP",
                        "expiration": "2026-07-17",
                        "strikes": "475",
                        "target_quantity": 1,
                        "limit_credit": 1.2,
                    }
                ]
            },
            {
                "positions": [],
                "fills": [
                    {
                        "fill_id": "F-STAGED",
                        "ticket_id": "QTK-STAGED",
                        "ticker": "SPY",
                        "strategy": "CSP",
                        "quantity": 1,
                        "price": 1.22,
                        "filled_at": "2026-06-11T15:00:00+00:00",
                    }
                ],
            },
        )
        update = report["proposed_journal_updates"][0]["set"]
        self.assertEqual(update["status"], "OPEN")
        self.assertEqual(update["opened_at"], "2026-06-11T15:00:00+00:00")

    def test_csp_assignment_becomes_adjusted_basis_equity_lot(self):
        journal = {
            "trades": [
                {
                    "id": "T-CSP",
                    "ticket_id": "QTK-CSP",
                    "status": "OPEN",
                    "ticker": "SPY",
                    "strategy": "CSP",
                    "expiration": "2026-07-17",
                    "strikes": "475",
                    "quantity": 1,
                    "entry_credit": 2.5,
                    "entry_debit": 0,
                }
            ],
            "equity_lots": [],
        }
        events = [
            {
                "event_id": "ASSIGN-1",
                "event_type": "ASSIGNMENT",
                "occurred_at": "2026-07-18T01:00:00+00:00",
                "ticker": "SPY",
                "expiration": "2026-07-17",
                "option_type": "P",
                "strike": 475,
                "quantity": 100,
            }
        ]
        proposed = proposed_assignment_updates(journal, events)
        self.assertEqual(len(proposed), 1)
        self.assertEqual(proposed[0]["equity_lot"]["cost_basis_per_share"], 472.5)
        applied = apply_assignment_updates(journal, proposed)
        self.assertEqual(applied["journal"]["trades"][0]["status"], "CLOSED")
        self.assertEqual(applied["journal"]["trades"][0]["realized_pnl"], 0.0)
        lot = applied["journal"]["equity_lots"][0]
        self.assertEqual(lot["quantity"], 100)
        self.assertEqual(lot["broker_reported_quantity"], 100)
        self.assertTrue(lot["covered_call_ready"])
        self.assertEqual(apply_assignment_updates(applied["journal"], proposed)["applied_assignment_updates"], [])

    def test_reconciliation_aggregates_partial_fills_by_target_quantity(self):
        tickets = {
            "tickets": [
                {
                    "ticket_id": "QTK-2",
                    "ticker": "SPY",
                    "strategy": "BULL_PUT",
                    "expiration": "2026-07-17",
                    "strikes": "475/470",
                    "target_quantity": 2,
                    "limit_credit": 1.10,
                }
            ]
        }
        fills = [
            {
                "fill_id": "F1",
                "ticker": "SPY",
                "strategy": "BULL_PUT",
                "expiration": "2026-07-17",
                "strikes": "475/470",
                "quantity": 1,
                "net_credit": 1.08,
                "filled_at": "2026-06-10T14:30:00+00:00",
            },
            {
                "fill_id": "F2",
                "ticker": "SPY",
                "strategy": "BULL_PUT",
                "expiration": "2026-07-17",
                "strikes": "475/470",
                "quantity": 1,
                "net_credit": 1.16,
                "filled_at": "2026-06-10T14:31:00+00:00",
            },
        ]
        report = build_reconciliation({"trades": []}, tickets, {"positions": [], "fills": fills})
        match = report["ticket_matches"][0]
        self.assertEqual(match["status"], "MATCHED")
        self.assertEqual(match["fill_id"], "F1")
        self.assertEqual(match["fill_ids"], ["F1", "F2"])
        self.assertEqual(match["filled_quantity"], 2)
        self.assertEqual(match["fill_price"], 1.12)
        self.assertEqual(report["summary"]["unmatched_fills"], 0)

    def test_reconciliation_keeps_incomplete_ticket_partial(self):
        journal = {
            "trades": [
                {
                    "id": "T-PARTIAL",
                    "ticket_id": "QTK-3",
                    "status": "OPEN",
                    "ticker": "SPY",
                    "strategy": "CSP",
                }
            ]
        }
        tickets = {
            "tickets": [
                {
                    "ticket_id": "QTK-3",
                    "ticker": "SPY",
                    "strategy": "CSP",
                    "expiration": "2026-07-17",
                    "strikes": "475",
                    "target_quantity": 2,
                    "limit_credit": 1.20,
                }
            ]
        }
        broker = {
            "positions": [],
            "fills": [
                {
                    "fill_id": "F-PARTIAL",
                    "ticker": "SPY",
                    "strategy": "CSP",
                    "expiration": "2026-07-17",
                    "strikes": "475",
                    "quantity": 1,
                    "net_credit": 1.22,
                }
            ],
        }
        report = build_reconciliation(journal, tickets, broker)
        match = report["ticket_matches"][0]
        self.assertEqual(match["status"], "PARTIAL")
        self.assertEqual(match["remaining_quantity"], 1)
        self.assertEqual(report["summary"]["partial_tickets"], 1)
        self.assertEqual(report["summary"]["matched_tickets"], 0)
        self.assertEqual(report["proposed_journal_updates"], [])

    def test_reconciliation_routes_closing_fill_to_open_trade(self):
        journal = {
            "trades": [
                {
                    "id": "T-CLOSE",
                    "ticket_id": "QTK-OPEN",
                    "status": "OPEN",
                    "ticker": "SPY",
                    "strategy": "BULL_PUT",
                    "expiration": "2026-07-17",
                    "strikes": "475/470",
                    "quantity": 1,
                    "entry_credit": 1.2,
                    "capital_at_risk": 380,
                }
            ]
        }
        close_fill = {
            "fill_id": "F-CLOSE",
            "ticker": "SPY",
            "strategy": "BULL_PUT",
            "expiration": "2026-07-17",
            "strikes": "475/470",
            "quantity": 1,
            "net_credit": -0.4,
            "fees": 1.3,
            "filled_at": "2026-06-10T15:30:00+00:00",
            "execution_effect": "CLOSE",
            "classification_confidence": "MEDIUM",
        }
        report = build_reconciliation(
            journal,
            {
                "tickets": [
                    {
                        "ticket_id": "QTK-NEW",
                        "ticker": "SPY",
                        "strategy": "BULL_PUT",
                        "expiration": "2026-07-17",
                        "strikes": "475/470",
                    }
                ]
            },
            {"positions": [], "fills": [close_fill]},
        )
        self.assertEqual(report["ticket_matches"][0]["status"], "UNMATCHED")
        self.assertEqual(report["summary"]["matched_exit_fills"], 1)
        self.assertEqual(report["summary"]["unmatched_fills"], 0)
        exit_match = report["trade_exit_matches"][0]
        self.assertEqual(exit_match["trade_id"], "T-CLOSE")
        self.assertEqual(exit_match["exit_price"], 0.4)
        proposal = report["proposed_exit_updates"][0]
        self.assertEqual(proposal["set"]["exit_debit"], 0.4)
        self.assertEqual(proposal["set"]["status"], "CLOSED")
        self.assertEqual(proposal["set"]["realized_pnl"], 78.7)
        self.assertEqual(proposal["set"]["realized_return_pct"], 20.71)
        self.assertFalse(proposal["apply_automatically"])

    def test_partial_closing_fill_does_not_propose_full_trade_close(self):
        report = build_reconciliation(
            {
                "trades": [
                    {
                        "id": "T-PARTIAL-EXIT",
                        "status": "OPEN",
                        "ticker": "SPY",
                        "strategy": "CSP",
                        "expiration": "2026-07-17",
                        "strikes": "475",
                        "quantity": 2,
                        "entry_credit": 1.2,
                    }
                ]
            },
            {"tickets": []},
            {
                "positions": [],
                "fills": [
                    {
                        "fill_id": "F-PARTIAL-EXIT",
                        "ticker": "SPY",
                        "strategy": "CSP",
                        "expiration": "2026-07-17",
                        "strikes": "475",
                        "quantity": 1,
                        "net_credit": -0.4,
                        "execution_effect": "CLOSE",
                    }
                ],
            },
        )
        self.assertEqual(report["trade_exit_matches"][0]["status"], "CLOSE_PARTIAL")
        self.assertEqual(report["proposed_exit_updates"], [])

    def test_reconciliation_flags_exact_ticket_overfill(self):
        report = build_reconciliation(
            {"trades": []},
            {
                "tickets": [
                    {
                        "ticket_id": "QTK-OVER",
                        "ticker": "SPY",
                        "strategy": "CSP",
                        "target_quantity": 1,
                    }
                ]
            },
            {
                "positions": [],
                "fills": [
                    {
                        "fill_id": "F-OVER-1",
                        "ticket_id": "QTK-OVER",
                        "ticker": "SPY",
                        "strategy": "CSP",
                        "quantity": 1,
                        "price": 1.2,
                    },
                    {
                        "fill_id": "F-OVER-2",
                        "ticket_id": "QTK-OVER",
                        "ticker": "SPY",
                        "strategy": "CSP",
                        "quantity": 1,
                        "price": 1.25,
                    },
                ],
            },
        )
        self.assertEqual(report["ticket_matches"][0]["status"], "OVERFILLED")
        self.assertEqual(report["ticket_matches"][0]["filled_quantity"], 2)
        self.assertEqual(report["summary"]["overfilled_tickets"], 1)
        self.assertEqual(report["summary"]["matched_tickets"], 1)

    def test_reconciliation_never_reassigns_fill_owned_by_another_ticket(self):
        report = build_reconciliation(
            {"trades": []},
            {
                "tickets": [
                    {
                        "ticket_id": "QTK-NEW",
                        "ticker": "SPY",
                        "strategy": "CSP",
                        "expiration": "2026-07-17",
                        "strikes": "475",
                    }
                ]
            },
            {
                "positions": [],
                "fills": [
                    {
                        "fill_id": "F-OWNED",
                        "ticket_id": "QTK-OLD",
                        "ticker": "SPY",
                        "strategy": "CSP",
                        "expiration": "2026-07-17",
                        "strikes": "475",
                        "quantity": 1,
                        "price": 1.2,
                    }
                ],
            },
        )
        self.assertEqual(report["ticket_matches"][0]["status"], "UNMATCHED")
        self.assertEqual(report["summary"]["unmatched_fills"], 1)

    def test_reconciliation_applies_journal_updates_explicitly(self):
        journal = {
            "trades": [
                {"id": "T1", "ticket_id": "QTK-1", "status": "OPEN", "ticker": "SPY", "strategy": "BULL_PUT"}
            ]
        }
        updates = [
            {
                "trade_id": "T1",
                "ticket_id": "QTK-1",
                "set": {"planned_limit_credit": 1.1, "entry_credit": 1.12, "broker_fill_id": "F1"},
            }
        ]
        applied = apply_journal_updates(journal, updates)
        trade = applied["journal"]["trades"][0]
        self.assertEqual(trade["entry_credit"], 1.12)
        self.assertEqual(trade["broker_fill_id"], "F1")
        self.assertEqual(len(applied["applied_updates"]), 1)

    def test_execution_analytics_measures_fill_quality(self):
        tickets = {
            "tickets": [
                {
                    "ticket_id": "QTK-1",
                    "ticker": "SPY",
                    "strategy": "BULL_PUT",
                    "execution_grade": "B",
                    "limit_credit": 1.10,
                    "do_not_chase_below": 1.00,
                },
                {
                    "ticket_id": "QTK-2",
                    "ticker": "QQQ",
                    "strategy": "CSP",
                    "execution_grade": "A",
                    "limit_credit": 2.00,
                    "do_not_chase_below": 1.80,
                },
            ]
        }
        reconciliation = {
            "ticket_matches": [
                {"ticket_id": "QTK-1", "status": "MATCHED", "fill_price": 1.12},
                {"ticket_id": "QTK-2", "status": "MATCHED", "fill_price": 1.75},
            ]
        }
        report = build_execution_analytics(tickets, reconciliation)
        self.assertEqual(report["summary"]["fill_rate"], 100.0)
        self.assertEqual(report["summary"]["avg_credit_improvement"], -0.115)
        self.assertEqual(report["summary"]["floor_violations"], 1)

    def test_execution_analytics_reports_quantity_fill_rate(self):
        tickets = {
            "tickets": [
                {
                    "ticket_id": "QTK-P",
                    "ticker": "SPY",
                    "strategy": "CSP",
                    "target_quantity": 2,
                    "limit_credit": 1.2,
                    "do_not_chase_below": 1.1,
                    "execution_grade": "B",
                }
            ]
        }
        reconciliation = {
            "ticket_matches": [
                {
                    "ticket_id": "QTK-P",
                    "status": "PARTIAL",
                    "target_quantity": 2,
                    "filled_quantity": 1,
                    "fill_count": 1,
                    "fill_price": 1.22,
                }
            ]
        }
        report = build_execution_analytics(tickets, reconciliation)
        self.assertEqual(report["summary"]["fill_rate"], 0.0)
        self.assertEqual(report["summary"]["quantity_fill_rate"], 50.0)
        self.assertEqual(report["summary"]["partial"], 1)
        self.assertEqual(report["by_strategy"]["CSP"]["quantity_fill_rate"], 50.0)

    def test_execution_analytics_tracks_fees_delay_and_persistent_ticket_matches(self):
        reconciliation = {
            "ticket_matches": [
                {
                    "ticket_id": "QTK-OLD",
                    "ticker": "SPY",
                    "strategy": "CSP",
                    "status": "MATCHED",
                    "target_quantity": 1,
                    "filled_quantity": 1,
                    "planned_limit_credit": 1.2,
                    "fill_price": 1.22,
                    "fees": 1.3,
                    "fill_delay_seconds": 90,
                }
            ]
        }
        report = build_execution_analytics({"tickets": []}, reconciliation)
        self.assertEqual(report["summary"]["tickets"], 1)
        self.assertEqual(report["summary"]["total_fees"], 1.3)
        self.assertEqual(report["summary"]["avg_fill_delay_seconds"], 90.0)
        self.assertEqual(report["summary"]["avg_credit_improvement"], 0.02)

    def test_execution_attribution_requires_samples_and_caps_penalty(self):
        insufficient = adjustment_for_summary(
            {
                "count": 4,
                "fill_rate": 0,
                "avg_credit_improvement": -1,
                "fees_per_contract": 10,
                "avg_fill_delay_seconds": 7200,
            },
            min_samples=5,
        )
        self.assertEqual(insufficient["signal"], "INSUFFICIENT")
        self.assertEqual(insufficient["score_adjustment"], 0)

        throttled = adjustment_for_summary(
            {
                "count": 10,
                "fill_rate": 20,
                "avg_credit_improvement": -0.5,
                "fees_per_contract": 10,
                "avg_fill_delay_seconds": 7200,
            },
            min_samples=5,
        )
        self.assertEqual(throttled["signal"], "THROTTLE")
        self.assertEqual(throttled["score_adjustment"], -5)
        self.assertEqual(throttled["size_multiplier"], 0.75)

    def test_execution_attribution_uses_latest_ticket_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "quant.db"
            con = connect(db_path)
            try:
                con.execute(
                    "INSERT INTO tickets(ticket_id, ticker, strategy, decision, "
                    "expiration, lifecycle_status, broker_order_id, submitted_at, payload_json, updated_at) "
                    "VALUES ('QTK-1', 'SPY', 'BULL_PUT', 'APPROVE', "
                    "'2026-07-17', 'SUBMITTED', 'ORDER-1', '2026-06-01T10:00:00Z', "
                    "'{}', '2026-06-01T10:00:00Z')"
                )
                con.commit()
                record_reconciliation(
                    con,
                    {
                        "created_at": "2026-06-01T10:00:00Z",
                        "ticket_matches": [
                            {
                                "ticket_id": "QTK-1",
                                "ticker": "SPY",
                                "strategy": "BULL_PUT",
                                "status": "PARTIAL",
                                "target_quantity": 2,
                                "filled_quantity": 1,
                                "planned_limit_credit": 1.2,
                                "fill_price": 1.15,
                                "fees": 1.0,
                            }
                        ],
                    },
                )
                record_reconciliation(
                    con,
                    {
                        "created_at": "2026-06-01T10:05:00Z",
                        "ticket_matches": [
                            {
                                "ticket_id": "QTK-1",
                                "ticker": "SPY",
                                "strategy": "BULL_PUT",
                                "status": "MATCHED",
                                "target_quantity": 2,
                                "filled_quantity": 2,
                                "planned_limit_credit": 1.2,
                                "fill_price": 1.18,
                                "fees": 2.0,
                            }
                        ],
                    },
                )
            finally:
                con.close()

            records = load_execution_records(db_path)
            report = build_execution_attribution(records, min_samples=1)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["status"], "MATCHED")
            self.assertEqual(report["summary"]["quantity_fill_rate"], 100.0)
            self.assertEqual(report["summary"]["fees_per_contract"], 1.0)

    def test_execution_attribution_counts_only_submitted_tickets(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "quant.db"
            con = connect(db_path)
            try:
                # Three recommendations exist, but only one was submitted.
                record_reconciliation(
                    con,
                    {
                        "created_at": "2026-06-01T10:00:00Z",
                        "ticket_matches": [
                            {
                                "ticket_id": "QTK-ACTIVE",
                                "ticker": "SPY",
                                "strategy": "BULL_PUT",
                                "status": "UNMATCHED",
                                "target_quantity": 1,
                                "filled_quantity": 0,
                            },
                            {
                                "ticket_id": "QTK-EXPIRED",
                                "ticker": "QQQ",
                                "strategy": "BULL_PUT",
                                "status": "UNMATCHED",
                                "target_quantity": 1,
                                "filled_quantity": 0,
                            },
                            {
                                "ticket_id": "QTK-CANCELLED",
                                "ticker": "NVDA",
                                "strategy": "BULL_PUT",
                                "status": "UNMATCHED",
                                "target_quantity": 1,
                                "filled_quantity": 0,
                            },
                        ],
                    },
                )
                # Seed the tickets table directly with the right lifecycle
                # statuses. upsert_tickets does not propagate lifecycle_status
                # on conflict (intentional: that field is governed by the
                # durable queue, not by the action plan), so we use raw SQL.
                for tid, status in [
                    ("QTK-ACTIVE", "SUBMITTED"),
                    ("QTK-EXPIRED", "EXPIRED"),
                    ("QTK-CANCELLED", "CANCELLED"),
                ]:
                    con.execute(
                        "INSERT INTO tickets(ticket_id, ticker, strategy, decision, "
                        "expiration, lifecycle_status, broker_order_id, submitted_at, payload_json, updated_at) "
                        "VALUES (?, 'X', 'BULL_PUT', 'APPROVE', '2026-07-17', ?, ?, ?, '{}', '2026-06-01T10:00:00Z')",
                        (
                            tid,
                            status,
                            "ORDER-ACTIVE" if tid == "QTK-ACTIVE" else None,
                            "2026-06-01T10:00:00Z" if tid == "QTK-ACTIVE" else None,
                        ),
                    )
                con.commit()
            finally:
                con.close()

            records = load_execution_records(db_path)
            self.assertEqual(len(records), 1, "only the explicitly submitted ticket should survive")
            self.assertEqual(records[0]["ticket_id"], "QTK-ACTIVE")
            # The summary must show n=1, not n=3 — otherwise THROTTLE fires
            # from stale unmatched records.
            report = build_execution_attribution(records, min_samples=1)
            self.assertEqual(report["summary"]["count"], 1)
            self.assertEqual(report["summary"]["fill_rate"], 0.0)  # 0/1 unmatched, not 0/3

    def test_ready_tickets_do_not_create_execution_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "quant.db"
            con = connect(db_path)
            try:
                upsert_tickets(
                    con,
                    [{"ticket_id": "QTK-READY", "ticker": "SPY", "strategy": "CSP"}],
                )
                record_reconciliation(
                    con,
                    {
                        "ticket_matches": [
                            {
                                "ticket_id": "QTK-READY",
                                "ticker": "SPY",
                                "strategy": "CSP",
                                "status": "UNMATCHED",
                            }
                        ]
                    },
                )
            finally:
                con.close()
            records = load_execution_records(db_path)
            report = build_execution_attribution(records, min_samples=1)
            self.assertEqual(records, [])
            self.assertEqual(report["summary"]["status"], "NO_SUBMITTED_HISTORY")
            self.assertEqual(adjustment_for_summary(report["summary"], 1)["signal"], "NO_SUBMITTED_HISTORY")

    def test_workflow_profiles_separate_planning_and_execution(self):
        planning = profile_skips("planning", Namespace())
        executable = profile_skips("executable", Namespace())
        self.assertTrue(planning["skip_tickets"])
        self.assertTrue(planning["skip_storage"])
        self.assertFalse(planning["skip_dashboard"])
        self.assertFalse(executable["skip_tickets"])
        self.assertFalse(executable["skip_storage"])
        self.assertTrue(executable["skip_discovery"])
        self.assertTrue(executable["skip_dashboard"])
        self.assertTrue(executable["skip_scorecard"])
        self.assertFalse(planning["skip_scorecard"])

    def test_action_plan_applies_execution_attribution(self):
        limits = RiskLimits(account_nav=30000)
        baseline = build_action_plan(sample_scan_report(), None, {"trades": []}, limits)
        attribution = {
            "strategy_adjustments": {
                "BULL_PUT": {
                    "score_adjustment": -4,
                    "size_multiplier": 0.75,
                    "signal": "THROTTLE",
                    "sample_size": 10,
                }
            }
        }
        adjusted = build_action_plan(
            sample_scan_report(),
            None,
            {"trades": []},
            limits,
            execution_attribution=attribution,
        )
        before = next(row for row in baseline["actions"] if row["strategy"] == "BULL_PUT")
        after = next(row for row in adjusted["actions"] if row["strategy"] == "BULL_PUT")
        self.assertEqual(after["score"], before["score"] - 4)
        self.assertLess(after["action_size_multiplier"], before["action_size_multiplier"])
        self.assertEqual(after["execution_attribution"]["signal"], "THROTTLE")

    def test_execution_attribution_never_overrides_hard_rejection(self):
        rejected = apply_performance_overlay(
            {
                "ticker": "SPY",
                "strategy": "BULL_PUT",
                "risk_decision": "REJECT",
                "size_multiplier": 0,
                "score": 70,
                "checks": [{"ok": False, "severity": "hard"}],
            },
            {},
            feedback={
                "execution_adjustments": {
                    "BULL_PUT": {
                        "score_adjustment": 5,
                        "size_multiplier": 1.05,
                        "signal": "BOOST",
                    }
                }
            },
        )
        self.assertEqual(rejected["action_decision"], "REJECT")
        self.assertEqual(rejected["action_size_multiplier"], 0)

    def test_dashboard_renders_core_sections(self):
        plan = {
            "summary": {"approve": 1, "reduce": 0, "reject": 0},
            "actions": [
                {
                    "action_decision": "APPROVE",
                    "ticker": "SPY",
                    "strategy": "BULL_PUT",
                    "score": 72,
                    "action_size_multiplier": 1.0,
                    "profile_signal": "NORMAL",
                    "correlation": {"note": "ok"},
                    "candidate": {
                        "execution": {
                            "suggested_limit_credit": 1.1,
                            "do_not_chase_below": 1.0,
                            "execution_grade": "B",
                        }
                    },
                }
            ],
        }
        alerts = {"summary": {"high": 1}, "alerts": [{"priority": "HIGH", "kind": "candidate", "title": "SPY", "detail": "Approved"}]}
        tickets = {"tickets": [{"decision": "APPROVE", "ticker": "SPY", "strategy": "BULL_PUT", "expiration": "2026-07-17"}]}
        manifest = {"created_at": "2026-06-05T12:00:00", "reports": {"plan": "plan.json"}}
        html = build_dashboard(
            plan=plan,
            alerts=alerts,
            tickets=tickets,
            manifest=manifest,
            base=Path("."),
            scenario_stress={
                "summary": {"worst_scenario": "crash", "worst_pnl": -7500, "worst_pnl_pct_nav": -15},
                "scenarios": [
                    {
                        "name": "crash",
                        "market_shock_pct": -15,
                        "vol_shock_pct": 25,
                        "estimated_pnl": -7500,
                        "estimated_pnl_pct_nav": -15,
                    }
                ],
            },
            allocation={
                "summary": {
                    "selected": 1,
                    "capital_allocated": 380,
                    "tail_budget_utilization_pct": 10,
                },
                "selected": [
                    {
                        "rank": 1,
                        "ticker": "SPY",
                        "strategy": "BULL_PUT",
                        "objective_score": 75,
                        "capital": 380,
                        "tail_loss": 247,
                        "delta_change": -22,
                    }
                ],
            },
            validation={
                "summary": {"status": "PASS", "avg_oos_expectancy": 25},
                "overall": {
                    "status": "PASS",
                    "valid_fold_count": 3,
                    "profitable_fold_pct": 66.7,
                    "avg_oos_expectancy": 25,
                    "avg_selected_threshold": 65,
                    "threshold_std": 2.5,
                },
            },
            drift={
                "summary": {"status": "STABLE", "severity": "LOW"},
                "comparison": {"expectancy_change": 5},
            },
            management={
                "summary": {
                    "open_trades": 1,
                    "high_urgency": 1,
                    "strike_threats": 1,
                    "event_spans": 1,
                },
                "actions": [
                    {
                        "urgency": "HIGH",
                        "ticker": "SPY",
                        "strategy": "CSP",
                        "dte": 12,
                        "unrealized_pnl_pct": -20,
                        "action": "ROLL_OR_CLOSE",
                        "strike_threat": {"status": "THREAT"},
                        "event_span": [{"event_type": "FOMC", "date": "2026-06-17"}],
                        "reasons": ["STRIKE_THREAT"],
                    }
                ],
            },
        )
        self.assertIn("Quant Tools Dashboard", html)
        self.assertIn("Action Plan", html)
        self.assertIn("Score-Band Performance", html)
        self.assertIn("Open Position Management", html)
        self.assertIn("ROLL_OR_CLOSE", html)
        self.assertIn("Strategy Calibration", html)
        self.assertIn("Execution Tickets", html)
        self.assertIn("Scenario Stress", html)
        self.assertIn("Portfolio Allocation", html)
        self.assertIn("Walk-Forward Validation", html)
        self.assertIn("Drift Status", html)
        self.assertIn("crash", html)
        self.assertIn("SPY", html)

    def test_config_deep_merge(self):
        merged = deep_merge({"a": {"b": 1, "c": 2}, "x": 3}, {"a": {"b": 9}})
        self.assertEqual(merged["a"]["b"], 9)
        self.assertEqual(merged["a"]["c"], 2)
        self.assertEqual(merged["x"], 3)

    def test_derive_live_account_nav_prefers_options_bp(self):
        """Live NAV derivation: options_bp (the right number for sizing option
        orders) wins over cash_only and buying_power (which is 2× cash on Reg-T
        margin accounts and would over-state the budget)."""
        from common import derive_live_account_nav

        # All three present: options_bp wins
        report = {"portfolio": {"options_bp": 30000.0, "cash_only": 28000.0, "buying_power": 60000.0}}
        self.assertEqual(derive_live_account_nav(report, default=50000.0), 30000.0)

        # cash_only only (no margin) — wins over the 2× Reg-T buying_power
        report = {"portfolio": {"cash_only": 30000.0, "buying_power": 60000.0}}
        self.assertEqual(derive_live_account_nav(report, default=50000.0), 30000.0)

        # buying_power only (no options_bp, no cash_only) — falls through
        report = {"portfolio": {"buying_power": 60000.0}}
        self.assertEqual(derive_live_account_nav(report, default=50000.0), 60000.0)

        # No report at all — config default wins
        self.assertEqual(derive_live_account_nav(None, default=50000.0), 50000.0)
        self.assertEqual(derive_live_account_nav({}, default=50000.0), 50000.0)
        self.assertEqual(derive_live_account_nav({"portfolio": {}}, default=50000.0), 50000.0)

        # Zero / None values are skipped (the broker can briefly report 0 BP)
        report = {"portfolio": {"options_bp": 0, "cash_only": 30000.0}}
        self.assertEqual(derive_live_account_nav(report, default=50000.0), 30000.0)

    def test_apply_sizing_mode_scales_caps(self):
        """Sizing mode scales the per-NAV capital / tail / ticker caps so the
        operator can apply a half-size rule (cautious) or 1.5x (aggressive)
        without editing config. Hard-caps at 100% NAV to prevent overflow."""
        from portfolio_allocator import apply_sizing_mode

        config = {
            "max_positions": 6,
            "max_total_capital_pct": 0.35,
            "max_tail_loss_pct": 0.08,
            "max_expected_shortfall_pct": 0.08,
            "max_ticker_capital_pct": 0.15,
            "max_group_exposure_pct": 0.35,
        }

        cautious = apply_sizing_mode(config, "cautious")
        self.assertAlmostEqual(cautious["max_total_capital_pct"], 0.175)  # 0.35 * 0.5
        self.assertAlmostEqual(cautious["max_tail_loss_pct"], 0.04)
        self.assertAlmostEqual(cautious["max_expected_shortfall_pct"], 0.04)
        self.assertAlmostEqual(cautious["max_ticker_capital_pct"], 0.075)
        self.assertEqual(cautious["sizing_mode"], "cautious")
        self.assertEqual(cautious["sizing_multiplier"], 0.5)
        # Untouched keys pass through
        self.assertEqual(cautious["max_positions"], 6)
        self.assertEqual(cautious["max_group_exposure_pct"], 0.35)

        normal = apply_sizing_mode(config, "normal")
        # normal returns the original dict (unchanged values + no metadata)
        self.assertIs(normal, config)

        aggressive = apply_sizing_mode(config, "aggressive")
        self.assertAlmostEqual(aggressive["max_total_capital_pct"], 0.525)  # 0.35 * 1.5
        self.assertEqual(aggressive["sizing_mode"], "aggressive")
        self.assertEqual(aggressive["sizing_multiplier"], 1.5)

        # Cap at 1.0: even with aggressive=1.5, can't request >100% NAV
        huge = {"max_total_capital_pct": 0.9}
        capped = apply_sizing_mode(huge, "aggressive")
        self.assertAlmostEqual(capped["max_total_capital_pct"], 1.0)

    def test_parse_regime_from_brief_extracts_verdict(self):
        """The cron reads the macro regime from daily_brief.py's `Verdict:`
        line. Parser must handle all 4 macro verdicts and fall back to None
        on a malformed line — never raise, since cron failures page the user."""
        from common import parse_regime_from_brief, derive_sizing_mode, REGIME_TO_SIZING

        # Live-style brief excerpt (the 2 leading spaces matter — the parser
        # looks for "Verdict:" anywhere in the line, not at col 0)
        brief_live = """☀️ MORNING BRIEF — Tue Jun 09, 13:45 ET

🎯 MACRO REGIME
  Score: 70/100  ██████████████░░░░░░
  Verdict: AGGRESSIVE: scale up short premium
   • VIX 20.93 in sweet spot (20-25)

📈 MARKETS
"""
        self.assertEqual(parse_regime_from_brief(brief_live), "AGGRESSIVE")
        self.assertEqual(derive_sizing_mode(brief_live), ("aggressive", "AGGRESSIVE"))

        # Other 3 verdicts
        for verdict, expected_mode in [
            ("FAVORABLE: normal sizing", ("normal", "FAVORABLE")),
            ("CAUTIOUS: half size, skip earnings names", ("cautious", "CAUTIOUS")),
            ("DEFENSIVE: cash > premium, wait for setup", ("cautious", "DEFENSIVE")),
        ]:
            brief = f"\n🎯 MACRO REGIME\n  Verdict: {verdict}\n"
            self.assertEqual(parse_regime_from_brief(brief), verdict.split(":", 1)[0])
            self.assertEqual(derive_sizing_mode(brief), expected_mode)

        # Case insensitivity (brief output is uppercase but be defensive)
        brief_lower = "  Verdict: aggressive: scale up short premium"
        self.assertEqual(parse_regime_from_brief(brief_lower), "AGGRESSIVE")

        # Missing / malformed — must NOT raise, must return (cautious, None)
        self.assertIsNone(parse_regime_from_brief(""))
        self.assertIsNone(parse_regime_from_brief("no verdict line here\nnothing\n"))
        self.assertIsNone(parse_regime_from_brief("  Verdict: \n"))  # empty after colon
        self.assertEqual(derive_sizing_mode(""), ("cautious", None))
        self.assertEqual(derive_sizing_mode("no verdict line"), ("cautious", None))

        # Unknown verdict — falls through to cautious but returns the unknown
        # token so the caller can log a drift warning
        brief_unknown = "  Verdict: BAZINGA: something weird"
        self.assertEqual(parse_regime_from_brief(brief_unknown), "BAZINGA")
        self.assertEqual(derive_sizing_mode(brief_unknown), ("cautious", "BAZINGA"))

        # REGIME_TO_SIZING must cover all 4 macro verdicts
        self.assertEqual(set(REGIME_TO_SIZING.keys()),
                         {"AGGRESSIVE", "FAVORABLE", "CAUTIOUS", "DEFENSIVE"})

    def test_pretrade_uses_live_account_nav_from_risk_report(self):
        """pretrade_check picks up NAV from the risk report when present,
        ignoring the (stale or default) CLI --account-nav. The actual NAV
        override happens in main() (via derive_live_account_nav) before
        evaluate_candidate is called — this test documents the split of
        responsibility and confirms evaluate_candidate is limits-driven."""
        from pretrade_check import evaluate_candidate, RiskLimits

        candidate = {
            "ticker": "AAPL",
            "strategy": "BULL_PUT",  # needed for candidate_max_loss to use max_loss
            "score": 60,
            "pop_pct": 70,
            "score_components": {"liquidity": 80},
            "max_loss": 1500,
            "capital_required": 1500,
        }
        portfolio_report = {
            "portfolio": {"options_bp": 60000.0, "cash_only": 60000.0, "buying_power": 120000.0},
            "risk": {},
            "demo": False,
        }
        # With account_nav=60000: limit = 60000 * 0.05 = 3000; max_loss 1500 passes
        limits_60k = RiskLimits(account_nav=60000.0)
        result = evaluate_candidate(candidate, portfolio_report, limits_60k)
        max_loss_check = next(c for c in result["checks"] if c["name"] == "max_trade_risk")
        self.assertTrue(max_loss_check["ok"])

        # With account_nav=20000: limit = 20000 * 0.05 = 1000; max_loss 1500 fails
        # This is the case the live-NAV derivation prevents: if the caller
        # forgot to update the NAV, the same trade would wrongly be approved.
        limits_tiny = RiskLimits(account_nav=20000.0)
        result2 = evaluate_candidate(candidate, portfolio_report, limits_tiny)
        max_loss_check2 = next(c for c in result2["checks"] if c["name"] == "max_trade_risk")
        self.assertFalse(max_loss_check2["ok"])

    def test_cache_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            import cache_utils

            original = cache_utils.CACHE_DIR
            cache_utils.CACHE_DIR = Path(tmp)
            try:
                calls = {"n": 0}

                def compute():
                    calls["n"] += 1
                    return {"value": 42}

                first = cached("unit", 60, compute, "SPY")
                second = cached("unit", 60, compute, "SPY")
                self.assertEqual(first, {"value": 42})
                self.assertEqual(second, {"value": 42})
                self.assertEqual(calls["n"], 1)
                self.assertEqual(read_cache("unit", "SPY", ttl_seconds=60), {"value": 42})
            finally:
                cache_utils.CACHE_DIR = original

    def test_retry_call_and_staleness(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("temporary")
            return "ok"

        value, meta = retry_call(flaky, source="unit", retries=1, base_delay=0, jitter=0)
        self.assertEqual(value, "ok")
        self.assertTrue(meta.ok)
        self.assertEqual(meta.attempts, 2)
        self.assertFalse(quote_is_stale(utc_now_iso(), max_age_seconds=60))
        self.assertTrue(quote_is_stale(None, max_age_seconds=60))

    def test_discovery_scoring_prefers_liquid_clean_setups(self):
        strong = score_discovery_metrics({
            "price": 120,
            "avg_volume": 8_000_000,
            "rv_21d_pct": 35,
            "trend_3m_pct": 8,
            "days_to_earnings": 30,
        })
        weak = score_discovery_metrics({
            "price": 10,
            "avg_volume": 100_000,
            "rv_21d_pct": 5,
            "trend_3m_pct": -40,
            "days_to_earnings": 2,
        })
        self.assertGreater(strong["discovery_score"], weak["discovery_score"])

    def test_operator_summary_creates_missing_report_dir(self):
        # Regression: scripts/operator_summary.py used to call output.write_text
        # without ensuring the parent dir existed, so passing a non-existent
        # --report-dir raised FileNotFoundError. Real callers from
        # quant.py operator / daily_workflow always pre-create the dir, so the
        # toolkit's own 142 tests missed it; the skill-level smoke test
        # verify_toolkit.py caught it. The fix is a single mkdir.
        with tempfile.TemporaryDirectory() as parent:
            missing = Path(parent) / "does" / "not" / "exist"
            self.assertFalse(missing.exists())
            old_argv = sys.argv
            try:
                sys.argv = [
                    "operator_summary.py",
                    "--report-dir",
                    str(missing),
                ]
                operator_summary_main()
            finally:
                sys.argv = old_argv
            written = missing / "operator_summary.md"
            self.assertTrue(missing.is_dir(), "report dir should be auto-created")
            self.assertTrue(written.is_file(), "operator_summary.md should be written")
            self.assertGreater(written.stat().st_size, 0)

    def test_daily_brief_includes_watchlist_stock_prices(self):
        # Regression: SCAN_DISCREPANCIES_2026-06-11 item #4 — the brief
        # had a hardcoded MARKETS list (SPY/QQQ/IWM/^VIX/^TNX/DXY/BTC/ETH)
        # with no individual stocks, so MSFT (and NVDA/AAPL/TSLA) never
        # appeared with a price. Operators couldn't sanity-check strikes
        # like "MSFT BULL_PUT 375/370" against spot. Fix adds a
        # "WATCHLIST STOCKS" block iterating over the watchlist minus the
        # indices already in MARKETS.
        import daily_brief

        captured = {
            "SPY": {"ticker": "SPY", "last": 600.0, "prev": 595.0, "chg": 5.0, "pct": 0.84},
            "QQQ": {"ticker": "QQQ", "last": 500.0, "prev": 495.0, "chg": 5.0, "pct": 1.01},
            "IWM": {"ticker": "IWM", "last": 200.0, "prev": 198.0, "chg": 2.0, "pct": 1.01},
            "MSFT": {"ticker": "MSFT", "last": 387.90, "prev": 397.36, "chg": -9.46, "pct": -2.38},
            "NVDA": {"ticker": "NVDA", "last": 202.39, "prev": 200.43, "chg": 1.96, "pct": 0.98},
            "AAPL": {"ticker": "AAPL", "last": 295.57, "prev": 291.57, "chg": 4.0, "pct": 1.37},
        }

        def fake_snapshot(sym: str) -> dict:
            return captured.get(sym, {"ticker": sym, "error": "no data"})

        with patch.object(daily_brief, "index_snapshot", side_effect=fake_snapshot), \
             patch.object(daily_brief, "get_upcoming_earnings", return_value=[]):
            # Stub the heavy IV + macro paths so the test is offline-only.
            with patch.object(daily_brief, "get_top_setups", return_value=[]):
                text = daily_brief.build_brief(["SPY", "QQQ", "NVDA", "AAPL", "MSFT", "TSLA"])

        self.assertIn("📊 WATCHLIST STOCKS", text)
        # Indices (SPY/QQQ/IWM) appear in the MARKETS block, not duplicated here.
        # The new block must show the stock-only symbols.
        for ticker in ("NVDA", "AAPL", "MSFT"):
            self.assertIn(ticker, text, f"{ticker} should appear in WATCHLIST STOCKS block")
        # Spot price must be rendered so operators can sanity-check strikes.
        self.assertIn("387.90", text, "MSFT spot should be in the brief")
        # TSLA wasn't in the fake snapshot — should be flagged as unavailable,
        # not silently dropped, so operators know data is missing.
        self.assertIn("TSLA", text)
        self.assertIn("(price unavailable)", text)
        # Indices (already in MARKETS) must NOT be duplicated in the stocks block.
        # They still appear in MARKETS, so just confirm they're not in the
        # WATCHLIST STOCKS section as stocks.
        watchlist_block = text.split("📊 WATCHLIST STOCKS", 1)[1].split("🎯 IV REGIME", 1)[0]
        for idx in ("SPY", "QQQ", "IWM"):
            self.assertNotIn(idx, watchlist_block, f"{idx} should be in MARKETS, not duplicated in WATCHLIST STOCKS")


if __name__ == "__main__":
    unittest.main()
