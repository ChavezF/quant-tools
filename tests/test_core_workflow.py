import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from cache_utils import cached, read_cache
from candidate_scoring import score_results
from common import parse_osi_expiration, parse_osi_parts, parse_osi_strike
from pretrade_check import RiskLimits, evaluate_report
from toolkit_config import deep_merge
from trade_journal import add_trade, close_trade, default_state, journal_stats


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
        self.assertIn(ranked[0]["verdict"], {"DEPLOY", "SMALL_SIZE", "WATCH", "SKIP"})

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

    def test_config_deep_merge(self):
        merged = deep_merge({"a": {"b": 1, "c": 2}, "x": 3}, {"a": {"b": 9}})
        self.assertEqual(merged["a"]["b"], 9)
        self.assertEqual(merged["a"]["c"], 2)
        self.assertEqual(merged["x"], 3)

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


if __name__ == "__main__":
    unittest.main()
