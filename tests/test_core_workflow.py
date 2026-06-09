import tempfile
import unittest
from argparse import Namespace
from datetime import date, timedelta
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from cache_utils import cached, read_cache
from candidate_scoring import score_results
from common import parse_osi_expiration, parse_osi_parts, parse_osi_strike
from data_reliability import quote_is_stale, retry_call, utc_now_iso
from correlation_risk import correlation_penalty
from execution_quality import execution_quality
from execution_analytics import build_execution_analytics
from performance_profiles import build_profiles, lookup_profile
from pretrade_check import RiskLimits, evaluate_report
from scan_optimizer import parse_wing_widths, select_expirations
from toolkit_config import deep_merge
from trade_journal import add_trade, close_trade, default_state, journal_stats
from action_plan import build_action_plan
from adaptive_sizing import adaptive_size
from alerts import build_alerts
from broker_reconciliation import apply_journal_updates, build_reconciliation
from dashboard import build_dashboard
from database_maintenance import backup_database, maintain_database, prune_backups
from drift_monitor import build_drift_report
from execution_tickets import build_tickets
from feedback_calibration import build_feedback_report
from historical_analytics import build_analytics
from opportunity_discovery import score_discovery_metrics
from operator_summary import build_summary
from portfolio_allocator import allocate_portfolio
from scenario_stress import build_scenario_report
from health_check import build_health_report
from walk_forward_validation import build_walk_forward_report
from storage import connect, export_journal_state, table_counts, upsert_tickets, upsert_trades


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
        self.assertEqual(tickets[0]["strikes"], "475/470")
        self.assertEqual(tickets[0]["portfolio_allocation"]["rank"], 1)
        self.assertTrue(tickets[0]["ticket_id"].startswith("QTK-"))

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
        )
        self.assertIn("Quant Tools Morning Review", text)
        self.assertIn("Recommended minimum score: 60.0", text)
        self.assertIn("Do not place orders without explicit confirmation", text)

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
                counts = table_counts(con)
                self.assertEqual(counts["trades"], 1)
                self.assertEqual(counts["tickets"], 1)
                self.assertEqual(export_journal_state(con)["trades"][0]["status"], "CLOSED")
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
        )
        self.assertIn("Quant Tools Dashboard", html)
        self.assertIn("Action Plan", html)
        self.assertIn("Score-Band Performance", html)
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
            "max_ticker_capital_pct": 0.15,
            "max_group_exposure_pct": 0.35,
        }

        cautious = apply_sizing_mode(config, "cautious")
        self.assertAlmostEqual(cautious["max_total_capital_pct"], 0.175)  # 0.35 * 0.5
        self.assertAlmostEqual(cautious["max_tail_loss_pct"], 0.04)
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


if __name__ == "__main__":
    unittest.main()
