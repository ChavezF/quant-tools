#!/usr/bin/env python3.12
"""
position_tracker.py — Daily mark-to-market + roll suggestions for Public.com.

Workflow:
  1. Fetch live positions from Public.com
  2. Compare to local state file (state/positions.json) for cost basis
  3. Mark-to-market: current value, P&L per position, total
  4. Fetch live Greeks for any options
  5. Compute aggregate portfolio delta/gamma/theta/vega
  6. Roll suggestions:
     - Any option < 21 DTE and not yet at 50% profit → consider close or roll
     - Any option > 50% profit → consider close to lock in
     - Any option with delta drift > 0.10 from entry → flag
  7. Update state file

If positions exist in account but no state file (first run), records cost basis
from current value (assumes today's open) and warns.

Usage:
  ./position_tracker.py              # full report
  ./position_tracker.py --json      # JSON output
  ./position_tracker.py --init      # reset state file (use after manual trades)
"""
import argparse
import json
import sys
from datetime import datetime, date

from common import (
    STATE_DIR,
    atomic_write_json,
    configure_public_imports,
    get_public_client,
    greeks_to_dict,
    parse_osi_expiration,
    state_lock,
)

configure_public_imports()

STATE_FILE = STATE_DIR / "positions.json"


def get_client():
    return get_public_client()


def load_state() -> dict:
    """Load position state from local JSON file. Returns {positions: {symbol: {...}}}."""
    if not STATE_FILE.exists():
        return {"positions": {}, "last_updated": None, "history": []}
    try:
        return json.loads(STATE_FILE.read_text())
    except json.JSONDecodeError as exc:
        # Cost-basis history is unrecoverable if we overwrite it with a fresh
        # default state, so refuse instead of silently starting over.
        raise SystemExit(
            f"Position state {STATE_FILE} is corrupt ({exc}). Refusing to "
            "continue. Repair or remove the file (use --init to deliberately "
            "re-baseline cost basis) and rerun."
        ) from exc


def save_state(state: dict):
    atomic_write_json(STATE_FILE, state)


def fetch_portfolio(client) -> list[dict]:
    """Fetch live positions from Public.com."""
    try:
        p = client.get_portfolio()
        positions = []
        for pos in (p.positions or []):
            inst = pos.instrument
            positions.append({
                "symbol": inst.symbol,
                "name": inst.name,
                "type": inst.type.value,
                "quantity": float(pos.quantity),
                "current_value": float(pos.current_value) if pos.current_value else 0.0,
                "last_price": float(pos.last_price.last_price) if pos.last_price and pos.last_price.last_price else None,
            })
        return positions
    except Exception as e:
        print(f"  ! portfolio fetch failed: {e}", file=sys.stderr)
        return []


def fetch_greeks(client, osi: str) -> dict:
    """Fetch Greeks for a single option. Returns {delta, gamma, theta, vega, iv}."""
    try:
        res = client.get_option_greeks(osi_symbols=[osi])
        if res and res.greeks:
            gv = res.greeks[0].greeks
            return greeks_to_dict(gv)
    except Exception:
        pass
    return {"delta": 0, "gamma": 0, "theta": 0, "vega": 0, "iv": 0}


def track_positions(client, state: dict, init: bool = False) -> dict:
    """Update state with current positions. Returns a report dict."""
    positions = fetch_portfolio(client)
    state_pos = state.get("positions", {})

    # Mark each position
    report_positions = []
    aggregate_delta = 0.0
    aggregate_gamma = 0.0
    aggregate_theta = 0.0
    aggregate_vega = 0.0

    for pos in positions:
        sym = pos["symbol"]
        if sym not in state_pos or init:
            # New position — record cost basis = current value
            state_pos[sym] = {
                "symbol": sym,
                "name": pos["name"],
                "type": pos["type"],
                "quantity": pos["quantity"],
                "entry_value": pos["current_value"],
                "entry_date": str(date.today()),
                "entry_price": pos["last_price"],
            }
        else:
            # Existing — update quantity (might have been adjusted)
            state_pos[sym]["quantity"] = pos["quantity"]

        sp = state_pos[sym]
        pnl = pos["current_value"] - sp["entry_value"]
        pnl_pct = (pnl / sp["entry_value"] * 100) if sp["entry_value"] else 0

        # Greeks for options
        greeks = {}
        if pos["type"] == "OPTION":
            greeks = fetch_greeks(client, sym)
            # Aggregate: 1 contract = 100 shares
            sign = 1 if pos["quantity"] > 0 else -1
            aggregate_delta += greeks.get("delta", 0) * abs(pos["quantity"]) * 100 * sign
            aggregate_gamma += greeks.get("gamma", 0) * abs(pos["quantity"]) * 100 * sign
            aggregate_theta += greeks.get("theta", 0) * abs(pos["quantity"]) * 100 * sign
            aggregate_vega += greeks.get("vega", 0) * abs(pos["quantity"]) * 100 * sign
        else:
            # Equity: delta = 1 per share
            aggregate_delta += pos["quantity"]

        # DTE calculation
        dte = None
        roll_action = None
        if pos["type"] == "OPTION":
            exp_str = parse_osi_expiration(sym)
            if exp_str:
                try:
                    exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
                    dte = (exp_date - date.today()).days
                except ValueError:
                    pass

        # Roll suggestion logic
        if dte is not None:
            if dte <= 21 and pnl_pct < 50:
                roll_action = "CLOSE or ROLL (≤21 DTE, <50% profit)"
            elif pnl_pct >= 50:
                roll_action = "CLOSE (50% profit target hit)"
            elif dte <= 7:
                roll_action = "URGENT: 1 week to expiry"

        report_positions.append({
            "symbol": sym,
            "type": pos["type"],
            "quantity": pos["quantity"],
            "current_value": pos["current_value"],
            "entry_value": sp["entry_value"],
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "dte": dte,
            "greeks": greeks,
            "roll_action": roll_action,
        })

    # Remove closed positions from state
    live_symbols = {p["symbol"] for p in positions}
    closed = [sym for sym in state_pos if sym not in live_symbols]
    for sym in closed:
        # Log the close
        history = state.setdefault("history", [])
        history.append({
            "symbol": sym,
            "close_date": str(date.today()),
            "final_pnl": round(state_pos[sym].get("entry_value", 0), 2),
        })
        del state_pos[sym]

    state["positions"] = state_pos
    state["last_updated"] = datetime.now().isoformat()

    total_value = sum(p["current_value"] for p in positions)
    total_cost = sum(sp["entry_value"] for sp in state_pos.values())
    total_pnl = total_value - total_cost

    return {
        "as_of": datetime.now().isoformat(),
        "positions": report_positions,
        "closed_recently": closed,
        "aggregate": {
            "net_delta": round(aggregate_delta, 2),
            "net_gamma": round(aggregate_gamma, 4),
            "net_theta_per_day": round(aggregate_theta, 2),
            "net_vega_per_1pct_iv": round(aggregate_vega, 2),
            "total_value": round(total_value, 2),
            "total_cost_basis": round(total_cost, 2),
            "total_pnl": round(total_pnl, 2),
            "total_pnl_pct": round((total_pnl / total_cost * 100) if total_cost else 0, 2),
        },
    }


def print_report(report: dict):
    print(f"\n{'#'*78}")
    print(f"# POSITION TRACKER — {report['as_of']}")
    print(f"{'#'*78}\n")

    if not report["positions"]:
        print("  No open positions.")
        return

    print(f"  {'Symbol':<22} {'Type':<7} {'Qty':>6} {'Value':>10} {'Cost':>10} {'PnL':>10} {'PnL%':>7} {'DTE':>4}  Action")
    print(f"  {'-'*22} {'-'*7} {'-'*6} {'-'*10} {'-'*10} {'-'*10} {'-'*7} {'-'*4}  ------")
    for p in report["positions"]:
        dte = f"{p['dte']}" if p["dte"] is not None else "  -"
        action = p.get("roll_action") or ""
        sign = "+" if p["pnl"] >= 0 else ""
        print(f"  {p['symbol']:<22} {p['type']:<7} {p['quantity']:>6.0f} "
              f"${p['current_value']:>9,.2f} ${p['entry_value']:>9,.2f} "
              f"{sign}${p['pnl']:>8,.2f} {sign}{p['pnl_pct']:>5.1f}% {dte:>4}  {action}")

    a = report["aggregate"]
    print(f"\n{'─'*78}")
    print("  TOTAL")
    print(f"  Value:        ${a['total_value']:,.2f}")
    print(f"  Cost basis:   ${a['total_cost_basis']:,.2f}")
    sign = "+" if a['total_pnl'] >= 0 else ""
    print(f"  P&L:          {sign}${a['total_pnl']:,.2f}  ({sign}{a['total_pnl_pct']:.2f}%)")
    print("\n  AGGREGATE GREEKS")
    print(f"  Net delta:    {a['net_delta']:>+10.1f} share-equivalent")
    print(f"  Net gamma:    {a['net_gamma']:>+10.4f}")
    print(f"  Net theta:    ${a['net_theta_per_day']:>+9.2f} per day")
    print(f"  Net vega:     ${a['net_vega_per_1pct_iv']:>+9.2f} per 1% IV move")

    if report["closed_recently"]:
        print(f"\n  Recently closed: {', '.join(report['closed_recently'])}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--init", action="store_true", help="Reset state file, capture current as cost basis")
    args = ap.parse_args()

    client = get_client()
    with state_lock("positions"):
        state = load_state()
        if args.init:
            state = {"positions": {}, "last_updated": None, "history": []}
            print("  State reset — current positions will be marked as 'entry' today.")

        report = track_positions(client, state, init=args.init)
        save_state(state)

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print_report(report)


if __name__ == "__main__":
    main()
