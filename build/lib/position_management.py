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
from datetime import date, datetime
from pathlib import Path
from typing import Any

from trade_journal import DEFAULT_STATE_FILE, load_journal


DEFAULT_THRESHOLDS = {
    "profit_target_pct": 50.0,
    "stop_loss_pct": 200.0,
    "manage_dte": 21,
    "urgent_dte": 7,
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
) -> dict[str, Any]:
    """Apply the management rules to one open trade."""
    dte = trade_dte(trade, today)
    pnl_pct = trade.get("unrealized_pnl_pct")
    pnl_pct = float(pnl_pct) if pnl_pct is not None else None

    action = "HOLD"
    urgency = "LOW"
    reasons = []

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
    }


def build_management_report(
    state: dict[str, Any],
    thresholds: dict[str, Any] | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    today = today or date.today()
    actions = [
        evaluate_trade(trade, thresholds, today)
        for trade in state.get("trades", [])
        if trade.get("status") == "OPEN"
    ]
    urgency_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    actions.sort(key=lambda row: (urgency_order.get(row["urgency"], 9), row["dte"] if row["dte"] is not None else 9999))
    return {
        "as_of": datetime.now().isoformat(),
        "today": today.isoformat(),
        "thresholds": thresholds,
        "summary": {
            "open_trades": len(actions),
            "close": sum(1 for row in actions if row["action"] == "CLOSE"),
            "roll_or_close": sum(1 for row in actions if row["action"] == "ROLL_OR_CLOSE"),
            "review": sum(1 for row in actions if row["action"] == "REVIEW"),
            "hold": sum(1 for row in actions if row["action"] == "HOLD"),
            "high_urgency": sum(1 for row in actions if row["urgency"] == "HIGH"),
        },
        "actions": actions,
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
    ap.add_argument("--journal", default=str(DEFAULT_STATE_FILE))
    ap.add_argument("--db", help="Optional SQLite database; authoritative over the JSON journal when set")
    ap.add_argument("--profit-target-pct", type=float, default=DEFAULT_THRESHOLDS["profit_target_pct"])
    ap.add_argument("--stop-loss-pct", type=float, default=DEFAULT_THRESHOLDS["stop_loss_pct"])
    ap.add_argument("--manage-dte", type=int, default=DEFAULT_THRESHOLDS["manage_dte"])
    ap.add_argument("--urgent-dte", type=int, default=DEFAULT_THRESHOLDS["urgent_dte"])
    ap.add_argument("--output")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    report = build_management_report(
        load_journal(Path(args.journal), args.db),
        thresholds={
            "profit_target_pct": args.profit_target_pct,
            "stop_loss_pct": args.stop_loss_pct,
            "manage_dte": args.manage_dte,
            "urgent_dte": args.urgent_dte,
        },
    )
    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=2, default=str))
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print_report(report)


if __name__ == "__main__":
    main()
