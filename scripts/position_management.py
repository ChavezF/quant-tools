#!/usr/bin/env python3.12
"""Exit and roll management for open journal trades.

The entry pipeline (scan → score → plan → ticket) decides what to open; this
module decides what to do with what is already open. It consumes the journal
after mark_to_market has stamped unrealized P&L and applies the standing
short-premium management rules:

  TAKE_PROFIT   unrealized_pnl_pct >= profit_target_pct (default 50% of max
                profit) → close and redeploy.
  STOP_LOSS     unrealized_pnl_pct <= -stop_loss_pct (default 200 = the loss
                equals 2x the credit received) → close; the trade is broken.
  MANAGE_DTE    dte <= manage_dte (default 21) without the profit target hit
                → close or roll before gamma risk dominates.
  URGENT        dte <= urgent_dte (default 7) escalates whatever action is on
                the table to HIGH urgency.
  REVIEW        the trade cannot be evaluated (no mark and no expiration) —
                surface it instead of silently holding.

Like every other tool here this only *recommends*: it never places or cancels
orders. Output feeds the operator report next to the entry plan.

Usage:
  ./position_management.py --journal state/trades.json --db state/quant_tools.db
  ./position_management.py --journal state/trades.json --json
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from broker_reconciliation import reconcile_open_trades
from mark_to_market import trade_legs
from toolkit_config import add_config_argument, load_config
from trade_journal import DEFAULT_STATE_FILE, load_journal


DEFAULT_THRESHOLDS = {
    "profit_target_pct": 50.0,
    "stop_loss_pct": 200.0,
    "manage_dte": 21,
    "urgent_dte": 7,
    "strike_threat_sigma": 0.5,
    "roll_min_dte": 28,
    "roll_max_dte": 50,
}


def trade_dte(trade: dict[str, Any], today: date) -> int | None:
    raw = str(trade.get("expiration") or "")[:10]
    if not raw:
        return None
    try:
        return (date.fromisoformat(raw) - today).days
    except ValueError:
        return None


def evaluate_trade(
    trade: dict[str, Any],
    thresholds: dict[str, Any],
    today: date,
    market: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply the management rules to one open trade."""
    dte = trade_dte(trade, today)
    pnl_pct = trade.get("unrealized_pnl_pct")
    pnl_pct = float(pnl_pct) if pnl_pct is not None else None

    action = "HOLD"
    urgency = "LOW"
    reasons = []
    market = market or {}

    if pnl_pct is not None and pnl_pct <= -float(thresholds["stop_loss_pct"]):
        action = "CLOSE"
        urgency = "HIGH"
        reasons.append(
            f"STOP_LOSS: unrealized {pnl_pct:.1f}% of max profit breaches "
            f"-{float(thresholds['stop_loss_pct']):.0f}% stop"
        )
    elif pnl_pct is not None and pnl_pct >= float(thresholds["profit_target_pct"]):
        action = "CLOSE"
        urgency = "HIGH"
        reasons.append(
            f"TAKE_PROFIT: {pnl_pct:.1f}% of max profit captured "
            f"(target {float(thresholds['profit_target_pct']):.0f}%)"
        )
    elif dte is not None and dte <= int(thresholds["manage_dte"]):
        action = "ROLL_OR_CLOSE"
        urgency = "MEDIUM"
        reasons.append(
            f"MANAGE_DTE: {dte} DTE <= {int(thresholds['manage_dte'])} and profit target not reached"
        )
    elif pnl_pct is None and dte is None:
        action = "REVIEW"
        reasons.append("no mark and no expiration on record; cannot evaluate")
    elif pnl_pct is None:
        action = "REVIEW"
        reasons.append("no mark-to-market yet; run `quant.py mark` for P&L-based rules")

    if dte is not None and dte <= int(thresholds["urgent_dte"]) and action != "HOLD":
        urgency = "HIGH"
        reasons.append(f"URGENT: {dte} DTE <= {int(thresholds['urgent_dte'])}")

    threat = strike_threat(trade, market, dte, float(thresholds["strike_threat_sigma"]))
    if threat["status"] in {"THREAT", "BREACHED"}:
        urgency = "HIGH"
        if action == "HOLD":
            action = "ROLL_OR_CLOSE"
        reasons.append(threat["reason"])

    events = event_span(trade, market.get("events", []), today)
    if events:
        urgency = (
            "HIGH"
            if any(event["event_type"] == "EARNINGS" for event in events)
            else max_urgency(urgency, "MEDIUM")
        )
        if action == "HOLD":
            action = "REVIEW"
        reasons.append(
            "EVENT_SPAN: " + ", ".join(f"{event['event_type']} {event['date']}" for event in events)
        )

    return {
        "trade_id": trade.get("id"),
        "ticket_id": trade.get("ticket_id"),
        "ticker": trade.get("ticker"),
        "strategy": trade.get("strategy"),
        "expiration": trade.get("expiration"),
        "dte": dte,
        "unrealized_pnl": trade.get("unrealized_pnl"),
        "unrealized_pnl_pct": pnl_pct,
        "marked_at": trade.get("marked_at"),
        "action": action,
        "urgency": urgency,
        "reasons": reasons,
        "market": {
            "spot": market.get("spot"),
            "iv": market.get("iv"),
        },
        "strike_threat": threat,
        "event_span": events,
        "roll_proposal": market.get("roll_proposal"),
    }


def max_urgency(left: str, right: str) -> str:
    rank = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
    return left if rank.get(left, 0) >= rank.get(right, 0) else right


def short_option_identity(trade: dict[str, Any]) -> tuple[str, float] | None:
    legs, _ = trade_legs(trade)
    if not legs:
        return None
    short_legs = [piece for piece in legs if piece["side"] == "SHORT"]
    if not short_legs:
        return None
    if len(short_legs) == 1:
        piece = short_legs[0]
        return str(piece["option_type"]), float(piece["strike"])
    return None


def strike_threat(
    trade: dict[str, Any],
    market: dict[str, Any],
    dte: int | None,
    warning_sigma: float,
) -> dict[str, Any]:
    identity = short_option_identity(trade)
    try:
        spot = float(market.get("spot"))
        iv = float(market.get("iv"))
    except (TypeError, ValueError):
        return {"status": "UNKNOWN", "sigma_distance": None, "reason": "spot or IV unavailable"}
    if not identity or spot <= 0 or iv <= 0 or dte is None:
        return {"status": "UNKNOWN", "sigma_distance": None, "reason": "short strike risk unavailable"}
    option_type, strike = identity
    one_sigma = spot * iv * math.sqrt(max(dte, 1) / 365)
    distance = spot - strike if option_type == "P" else strike - spot
    sigma_distance = distance / one_sigma if one_sigma else None
    if sigma_distance is None:
        status = "UNKNOWN"
    elif sigma_distance <= 0:
        status = "BREACHED"
    elif sigma_distance <= warning_sigma:
        status = "THREAT"
    else:
        status = "CLEAR"
    reason = (
        f"STRIKE_{status}: spot {spot:.2f}, short {option_type}{strike:g}, "
        f"distance {sigma_distance:.2f}σ"
        if sigma_distance is not None
        else "short strike risk unavailable"
    )
    return {
        "status": status,
        "option_type": option_type,
        "short_strike": strike,
        "spot": spot,
        "iv": iv,
        "one_sigma_move": round(one_sigma, 4),
        "sigma_distance": round(sigma_distance, 3) if sigma_distance is not None else None,
        "warning_sigma": warning_sigma,
        "reason": reason,
    }


def event_span(trade: dict[str, Any], events: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    expiration = trade_dte(trade, today)
    if expiration is None:
        return []
    end = today + timedelta(days=expiration)
    ticker = str(trade.get("ticker") or "").upper()
    out = []
    for event in events:
        try:
            event_date = date.fromisoformat(str(event.get("date") or "")[:10])
        except ValueError:
            continue
        event_ticker = str(event.get("ticker") or "").upper()
        if event_ticker and event_ticker != ticker:
            continue
        if today <= event_date <= end:
            out.append(
                {
                    "event_type": str(event.get("event_type") or "EVENT").upper(),
                    "date": event_date.isoformat(),
                    "ticker": event_ticker or None,
                    "detail": event.get("detail"),
                }
            )
    return sorted(out, key=lambda row: (row["date"], row["event_type"]))


def build_management_report(
    state: dict[str, Any],
    thresholds: dict[str, Any] | None = None,
    today: date | None = None,
    market_contexts: dict[str, dict[str, Any]] | None = None,
    broker_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    today = today or date.today()
    open_trades = [
        trade for trade in state.get("trades", [])
        if trade.get("status") == "OPEN"
    ]
    position_rows = (
        reconcile_open_trades(
            open_trades,
            broker_snapshot.get("positions", []),
            positions_available=bool(broker_snapshot.get("positions_available", False)),
        )
        if broker_snapshot is not None
        else []
    )
    positions_by_trade = {
        str(row.get("trade_id")): row
        for row in position_rows
    }
    actions = []
    for trade in open_trades:
        position = positions_by_trade.get(str(trade.get("id")))
        if position and position["status"] != "POSITION_FOUND":
            status = position["status"]
            if status == "MISSING_POSITION":
                reason = "PHANTOM: journal trade has no matching broker position"
                urgency = "HIGH"
            elif status == "PARTIAL_POSITION":
                reason = "BROKER_MISMATCH: only part of the journal structure is held"
                urgency = "HIGH"
            else:
                reason = "BROKER_UNKNOWN: position snapshot unavailable; management action withheld"
                urgency = "MEDIUM"
            actions.append(
                {
                    **evaluate_trade(
                        trade,
                        thresholds,
                        today,
                        (market_contexts or {}).get(str(trade.get("id")), {}),
                    ),
                    "action": "REVIEW",
                    "urgency": urgency,
                    "reasons": [reason],
                    "broker_position_status": status,
                    "position_symbols": position.get("position_symbols", []),
                }
            )
            continue
        row = evaluate_trade(
            trade,
            thresholds,
            today,
            (market_contexts or {}).get(str(trade.get("id")), {}),
        )
        if position:
            row["broker_position_status"] = position["status"]
            row["position_symbols"] = position.get("position_symbols", [])
        actions.append(row)
    urgency_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    actions.sort(key=lambda row: (urgency_order.get(row["urgency"], 9), row["dte"] if row["dte"] is not None else 9999))
    return {
        "as_of": datetime.now().isoformat(),
        "today": today.isoformat(),
        "broker_snapshot": {
            "snapshot_at": (broker_snapshot or {}).get("snapshot_at"),
            "positions_available": (broker_snapshot or {}).get("positions_available"),
            "source": (broker_snapshot or {}).get("source"),
        },
        "thresholds": thresholds,
        "summary": {
            "open_trades": len(actions),
            "close": sum(1 for row in actions if row["action"] == "CLOSE"),
            "roll_or_close": sum(1 for row in actions if row["action"] == "ROLL_OR_CLOSE"),
            "review": sum(1 for row in actions if row["action"] == "REVIEW"),
            "hold": sum(1 for row in actions if row["action"] == "HOLD"),
            "high_urgency": sum(1 for row in actions if row["urgency"] == "HIGH"),
            "strike_threats": sum(
                1 for row in actions if row["strike_threat"]["status"] in {"THREAT", "BREACHED"}
            ),
            "event_spans": sum(1 for row in actions if row["event_span"]),
            "credit_rolls": sum(
                1 for row in actions if (row.get("roll_proposal") or {}).get("status") == "CREDIT_AVAILABLE"
            ),
            "phantom_positions": sum(
                1 for row in actions if row.get("broker_position_status") == "MISSING_POSITION"
            ),
            "partial_positions": sum(
                1 for row in actions if row.get("broker_position_status") == "PARTIAL_POSITION"
            ),
            "unknown_positions": sum(
                1 for row in actions if row.get("broker_position_status") == "POSITION_UNKNOWN"
            ),
        },
        "actions": actions,
    }


class LiveManagementSource:
    """Cached Public/yfinance context for breach, event, and roll decisions."""

    def __init__(self, cfg: dict[str, Any], today: date | None = None):
        from common import get_public_client

        self.client = get_public_client()
        self.cfg = cfg
        self.today = today or date.today()
        self.reliability = cfg.get("data_reliability", {})
        self.thresholds = cfg.get("position_management", {})
        self._quotes: dict[str, float | None] = {}
        self._expirations: dict[str, list[str]] = {}
        self._chains: dict[tuple[str, str], dict[str, Any]] = {}
        self._earnings: dict[str, str | None] = {}

    def spot(self, ticker: str) -> float | None:
        if ticker not in self._quotes:
            from options_screener import fetch_quote

            quote = fetch_quote(self.client, ticker, self.reliability)
            self._quotes[ticker] = quote.get("last") or quote.get("bid")
        return self._quotes[ticker]

    def chain(self, ticker: str, expiration: str) -> dict[str, Any]:
        key = (ticker, expiration)
        if key not in self._chains:
            from options_screener import fetch_chain_with_greeks

            spot = self.spot(ticker)
            self._chains[key] = (
                fetch_chain_with_greeks(self.client, ticker, expiration, spot, reliability_cfg=self.reliability)
                if spot
                else {"calls": {}, "puts": {}}
            )
        return self._chains[key]

    def expirations(self, ticker: str) -> list[str]:
        if ticker not in self._expirations:
            from options_screener import fetch_option_expirations

            self._expirations[ticker] = fetch_option_expirations(self.client, ticker, self.reliability)
        return self._expirations[ticker]

    def earnings_date(self, ticker: str) -> str | None:
        if ticker not in self._earnings:
            from options_screener import fetch_underlying_metrics

            metrics = fetch_underlying_metrics(ticker, ttl_seconds=900)
            self._earnings[ticker] = metrics.get("earnings", {}).get("next")
        return self._earnings[ticker]

    def events(self, ticker: str) -> list[dict[str, Any]]:
        events = [
            {"event_type": "FOMC", "date": value, "detail": "Scheduled FOMC decision"}
            for value in self.thresholds.get("fomc_dates", [])
        ]
        earnings = self.earnings_date(ticker)
        if earnings:
            events.append({"event_type": "EARNINGS", "date": earnings, "ticker": ticker})
        return events

    def context(self, trade: dict[str, Any]) -> dict[str, Any]:
        ticker = str(trade.get("ticker") or "").upper()
        expiration = str(trade.get("expiration") or "")[:10]
        spot = self.spot(ticker)
        chain = self.chain(ticker, expiration) if spot and expiration else {"calls": {}, "puts": {}}
        identity = short_option_identity(trade)
        leg = {}
        if identity:
            option_type, strike = identity
            leg = chain.get("puts" if option_type == "P" else "calls", {}).get(strike, {})
        context = {
            "spot": spot,
            "iv": leg.get("iv"),
            "events": self.events(ticker),
        }
        dte = trade_dte(trade, self.today)
        threat = strike_threat(
            trade,
            context,
            dte,
            float(self.thresholds.get("strike_threat_sigma", 0.5)),
        )
        if (
            dte is not None
            and (
                dte <= int(self.thresholds.get("manage_dte", 21))
                or threat.get("status") in {"THREAT", "BREACHED"}
            )
        ):
            context["roll_proposal"] = self.roll_proposal(trade, chain)
        return context

    def roll_proposal(self, trade: dict[str, Any], current_chain: dict[str, Any]) -> dict[str, Any] | None:
        if trade_dte(trade, self.today) is None:
            return None
        legs, _ = trade_legs(trade)
        if not legs or len(legs) != 1:
            return {"status": "UNSUPPORTED", "reason": "defined-risk multi-leg roll requires broker combo support"}
        identity = short_option_identity(trade)
        if not identity:
            return {"status": "UNSUPPORTED", "reason": "roll proposals currently require one short option leg"}
        ticker = str(trade.get("ticker") or "").upper()
        current_expiration = str(trade.get("expiration") or "")[:10]
        candidates = []
        for raw in self.expirations(ticker):
            try:
                dte = (date.fromisoformat(raw) - self.today).days
            except ValueError:
                continue
            if (
                raw > current_expiration
                and int(self.thresholds.get("roll_min_dte", 28)) <= dte <= int(self.thresholds.get("roll_max_dte", 50))
            ):
                candidates.append((dte, raw))
        if not candidates:
            return {"status": "NO_NEXT_CYCLE", "reason": "no next expiration in configured roll window"}
        next_dte, next_expiration = min(candidates)
        option_type, current_strike = identity
        side = "puts" if option_type == "P" else "calls"
        current_leg = current_chain.get(side, {}).get(current_strike, {})
        target_delta = current_leg.get("delta")
        next_chain = self.chain(ticker, next_expiration)
        spot = self.spot(ticker) or 0
        eligible = [
            (strike, leg)
            for strike, leg in next_chain.get(side, {}).items()
            if (
                leg.get("delta") is not None
                and float(leg.get("bid") or 0) > 0
                and (
                    (option_type == "P" and float(strike) < spot * 1.005)
                    or (option_type == "C" and float(strike) > spot * 0.995)
                )
            )
        ]
        if target_delta is None or not eligible:
            return {"status": "NO_LIQUID_MATCH", "reason": "same-delta next-cycle strike unavailable"}
        next_strike, next_leg = min(
            eligible,
            key=lambda item: abs(float(item[1]["delta"]) - float(target_delta)),
        )
        close_debit = float(current_leg.get("ask") or current_leg.get("mark") or 0)
        open_credit = float(next_leg.get("bid") or 0)
        net_credit = open_credit - close_debit
        return {
            "status": "CREDIT_AVAILABLE" if net_credit > 0 else "DEBIT_ONLY",
            "from_expiration": current_expiration,
            "from_strike": current_strike,
            "to_expiration": next_expiration,
            "to_dte": next_dte,
            "to_strike": next_strike,
            "target_delta": round(float(target_delta), 4),
            "matched_delta": round(float(next_leg["delta"]), 4),
            "close_debit": round(close_debit, 4),
            "open_credit": round(open_credit, 4),
            "net_credit": round(net_credit, 4),
            "rule": "ROLL_ONLY_FOR_CREDIT",
        }


def print_report(report: dict[str, Any]) -> None:
    summary = report["summary"]
    print(f"\n{'#'*78}")
    print("# POSITION MANAGEMENT — open-trade exit/roll review")
    print(f"{'#'*78}\n")
    print(
        f"  Open={summary['open_trades']}  Close={summary['close']}  "
        f"Roll/Close={summary['roll_or_close']}  Review={summary['review']}  Hold={summary['hold']}"
    )
    if not report["actions"]:
        print("\n  No open trades to manage.")
        return
    print(f"\n  {'Trade':<14} {'Ticker':<6} {'Strategy':<12} {'DTE':>4} {'P&L%':>8}  {'Action':<14} Reason")
    for row in report["actions"]:
        dte = str(row["dte"]) if row["dte"] is not None else "-"
        pct = f"{row['unrealized_pnl_pct']:+.1f}" if row["unrealized_pnl_pct"] is not None else "-"
        reason = row["reasons"][0] if row["reasons"] else ""
        print(
            f"  {str(row['trade_id']):<14} {str(row['ticker']):<6} {str(row['strategy']):<12} "
            f"{dte:>4} {pct:>8}  {row['action']:<14} {reason}"
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    add_config_argument(ap)
    ap.add_argument("--journal", default=str(DEFAULT_STATE_FILE))
    ap.add_argument("--db", help="Optional SQLite database; authoritative over the JSON journal when set")
    ap.add_argument("--profit-target-pct", type=float)
    ap.add_argument("--stop-loss-pct", type=float)
    ap.add_argument("--manage-dte", type=int)
    ap.add_argument("--urgent-dte", type=int)
    ap.add_argument("--strike-threat-sigma", type=float)
    ap.add_argument("--no-live-context", action="store_true")
    ap.add_argument("--output")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.config)
    management_cfg = cfg.get("position_management", {})
    state = load_journal(Path(args.journal), args.db)
    thresholds = {
            "profit_target_pct": (
                args.profit_target_pct
                if args.profit_target_pct is not None
                else management_cfg.get("profit_target_pct", DEFAULT_THRESHOLDS["profit_target_pct"])
            ),
            "stop_loss_pct": (
                args.stop_loss_pct
                if args.stop_loss_pct is not None
                else management_cfg.get("stop_loss_pct", DEFAULT_THRESHOLDS["stop_loss_pct"])
            ),
            "manage_dte": (
                args.manage_dte
                if args.manage_dte is not None
                else management_cfg.get("manage_dte", DEFAULT_THRESHOLDS["manage_dte"])
            ),
            "urgent_dte": (
                args.urgent_dte
                if args.urgent_dte is not None
                else management_cfg.get("urgent_dte", DEFAULT_THRESHOLDS["urgent_dte"])
            ),
            "strike_threat_sigma": (
                args.strike_threat_sigma
                if args.strike_threat_sigma is not None
                else management_cfg.get("strike_threat_sigma", 0.5)
            ),
            "roll_min_dte": management_cfg.get("roll_min_dte", 28),
            "roll_max_dte": management_cfg.get("roll_max_dte", 50),
        }
    broker_snapshot = None
    if args.db:
        from storage import connect, load_latest_position_snapshot

        con = connect(args.db)
        try:
            broker_snapshot = load_latest_position_snapshot(con)
        finally:
            con.close()
    contexts = {}
    open_trades = [trade for trade in state.get("trades", []) if trade.get("status") == "OPEN"]
    if open_trades and not args.no_live_context:
        source = LiveManagementSource(cfg)
        contexts = {
            str(trade.get("id")): source.context(trade)
            for trade in open_trades
        }
    report = build_management_report(
        state,
        thresholds=thresholds,
        market_contexts=contexts,
        broker_snapshot=broker_snapshot,
    )
    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=2, default=str))
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print_report(report)


if __name__ == "__main__":
    main()
