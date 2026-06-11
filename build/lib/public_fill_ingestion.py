#!/usr/bin/env python3.12
"""Build a provider-neutral broker snapshot from Public.com account history."""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from common import PROJECT_ROOT, atomic_write_json, get_public_client, parse_osi_parts


DEFAULT_CURSOR = PROJECT_ROOT / "state" / "public_fill_cursor.json"
DEFAULT_SNAPSHOT = PROJECT_ROOT / "state" / "public_broker_snapshot.json"


def field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def enum_value(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "").upper()


def number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif value:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def timestamp_text(value: Any) -> str:
    parsed = parse_timestamp(value)
    return parsed.isoformat() if parsed else str(value or "")


def normalize_transaction(transaction: Any) -> dict[str, Any]:
    return {
        "id": str(field(transaction, "id", "") or ""),
        "timestamp": timestamp_text(field(transaction, "timestamp")),
        "type": enum_value(field(transaction, "type")),
        "sub_type": enum_value(field(transaction, "sub_type")),
        "account_number": str(field(transaction, "account_number", "") or ""),
        "symbol": str(field(transaction, "symbol", "") or "").upper(),
        "security_type": enum_value(field(transaction, "security_type")),
        "side": enum_value(field(transaction, "side")),
        "description": str(field(transaction, "description", "") or ""),
        "net_amount": number(field(transaction, "net_amount")),
        "principal_amount": number(field(transaction, "principal_amount")),
        "quantity": number(field(transaction, "quantity")),
        "direction": enum_value(field(transaction, "direction")),
        "fees": number(field(transaction, "fees")),
    }


def normalize_position(position: Any) -> dict[str, Any]:
    instrument = field(position, "instrument", {}) or {}
    last_price = field(position, "last_price", {}) or {}
    instrument_type = enum_value(field(instrument, "type"))
    symbol = str(field(instrument, "symbol", "") or "").upper()
    return {
        "symbol": symbol,
        "name": str(field(instrument, "name", "") or ""),
        "type": instrument_type,
        "quantity": number(field(position, "quantity")),
        "current_value": number(field(position, "current_value")),
        "last_price": (
            number(field(last_price, "last_price"))
            if field(last_price, "last_price") is not None
            else None
        ),
        "pct_of_portfolio": number(field(position, "percent_of_portfolio")),
        "osi": symbol if instrument_type == "OPTION" else None,
    }


def normalize_portfolio(portfolio: Any) -> dict[str, Any]:
    buying_power = field(portfolio, "buying_power", {}) or {}
    return {
        "positions": [
            normalize_position(position)
            for position in (field(portfolio, "positions", []) or [])
        ],
        "buying_power": number(field(buying_power, "buying_power")),
        "cash_only": number(field(buying_power, "cash_only_buying_power")),
        "options_bp": number(field(buying_power, "options_buying_power")),
    }


def signed_principal(transaction: dict[str, Any]) -> float:
    gross = abs(number(transaction.get("principal_amount")))
    if transaction.get("side") == "SELL":
        return gross
    if transaction.get("side") == "BUY":
        return -gross
    return number(transaction.get("principal_amount"))


def transaction_text(transaction: dict[str, Any]) -> str:
    return " ".join(
        str(transaction.get(key) or "").upper()
        for key in ("type", "sub_type", "description", "direction")
    )


def lifecycle_event_type(transaction: dict[str, Any]) -> str | None:
    text = transaction_text(transaction)
    for event_type, keywords in (
        ("ASSIGNMENT", ("ASSIGN", "ASSIGNED")),
        ("EXPIRATION", ("EXPIR", "EXPIRED")),
        ("EXERCISE", ("EXERCISE", "EXERCISED")),
    ):
        if any(keyword in text for keyword in keywords):
            return event_type
    return None


def classify_execution(
    transactions: list[dict[str, Any]],
    signed_total: float,
    option_fill: bool,
) -> tuple[str, str, str]:
    text = " ".join(transaction_text(transaction) for transaction in transactions)
    if any(keyword in text for keyword in ("TO OPEN", "OPENING", "OPENED")):
        return "OPEN", "HIGH", "description"
    if any(keyword in text for keyword in ("TO CLOSE", "CLOSING", "CLOSED")):
        return "CLOSE", "HIGH", "description"
    if option_fill:
        if signed_total > 0:
            return "OPEN", "MEDIUM", "short-premium net credit inference"
        if signed_total < 0:
            return "CLOSE", "MEDIUM", "short-premium net debit inference"
    if len(transactions) == 1 and transactions[0].get("security_type") == "EQUITY":
        side = transactions[0].get("side")
        if side == "BUY":
            return "OPEN", "LOW", "equity buy inference"
        if side == "SELL":
            return "CLOSE", "LOW", "equity sell inference"
    return "UNKNOWN", "LOW", "insufficient history fields"


def unit_price(transaction: dict[str, Any]) -> float | None:
    quantity = abs(number(transaction.get("quantity")))
    if quantity == 0:
        return None
    multiplier = 100 if transaction.get("security_type") == "OPTION" else 1
    return round(abs(number(transaction.get("principal_amount"))) / (quantity * multiplier), 4)


def transaction_leg(transaction: dict[str, Any]) -> dict[str, Any]:
    parsed = parse_osi_parts(transaction["symbol"])
    is_option = transaction.get("security_type") == "OPTION" or bool(parsed.get("option_type"))
    return {
        "transaction_id": transaction["id"],
        "symbol": transaction["symbol"],
        "ticker": parsed.get("underlying") if is_option else transaction["symbol"],
        "security_type": "OPTION" if is_option else transaction.get("security_type"),
        "side": transaction.get("side"),
        "quantity": abs(number(transaction.get("quantity"))),
        "price": unit_price(transaction),
        "principal_amount": number(transaction.get("principal_amount")),
        "fees": number(transaction.get("fees")),
        "expiration": parsed.get("expiration") if is_option else "",
        "option_type": parsed.get("option_type") if is_option else "",
        "strike": parsed.get("strike") if is_option else None,
    }


def infer_option_strategy(legs: list[dict[str, Any]]) -> tuple[str, str]:
    if len(legs) == 1:
        leg = legs[0]
        strike = f"{number(leg.get('strike')):g}" if leg.get("strike") is not None else ""
        if leg.get("option_type") == "P":
            return "CSP", strike
        if leg.get("option_type") == "C":
            return "CC", strike
        return "OPTION_TRADE", strike
    if len(legs) == 2 and all(leg.get("option_type") == "P" for leg in legs):
        higher, lower = sorted(legs, key=lambda leg: number(leg.get("strike")), reverse=True)
        if higher.get("side") != lower.get("side"):
            return "BULL_PUT", f"{higher['strike']:g}/{lower['strike']:g}"
    strikes = "/".join(
        f"{number(leg.get('strike')):g}"
        for leg in legs
        if leg.get("strike") is not None
    )
    return "OPTION_SPREAD", strikes


def build_fill(transactions: list[dict[str, Any]]) -> dict[str, Any]:
    legs = [transaction_leg(transaction) for transaction in transactions]
    first = transactions[0]
    option_fill = all(leg.get("security_type") == "OPTION" for leg in legs)
    strategy, strikes = infer_option_strategy(legs) if option_fill else ("EQUITY_TRADE", "")
    contracts = max((leg["quantity"] for leg in legs), default=0)
    signed_total = sum(signed_principal(transaction) for transaction in transactions)
    execution_effect, classification_confidence, classification_basis = classify_execution(
        transactions,
        signed_total,
        option_fill,
    )
    net_credit = (
        round(signed_total / (contracts * 100), 4)
        if option_fill and contracts
        else None
    )
    transaction_ids = [transaction["id"] for transaction in transactions]
    return {
        "fill_id": "PUBLIC-" + "-".join(transaction_ids),
        "transaction_ids": transaction_ids,
        "filled_at": first["timestamp"],
        "ticker": legs[0].get("ticker") if legs else "",
        "symbol": legs[0].get("symbol") if len(legs) == 1 else None,
        "security_type": "OPTION" if option_fill else first.get("security_type"),
        "strategy": strategy,
        "execution_effect": execution_effect,
        "classification_confidence": classification_confidence,
        "classification_basis": classification_basis,
        "expiration": legs[0].get("expiration") if option_fill else "",
        "strikes": strikes,
        "quantity": contracts if option_fill else abs(number(first.get("quantity"))),
        "net_credit": net_credit,
        "price": legs[0].get("price") if len(legs) == 1 else net_credit,
        "fees": round(sum(number(transaction.get("fees")) for transaction in transactions), 4),
        "legs": legs,
    }


def normalize_fills(transactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    trades = [transaction for transaction in transactions if transaction.get("type") == "TRADE"]
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for transaction in trades:
        leg = transaction_leg(transaction)
        if leg["security_type"] == "OPTION":
            key = (
                transaction.get("account_number", ""),
                transaction.get("timestamp", ""),
                leg.get("ticker", ""),
                leg.get("expiration", ""),
            )
        else:
            key = ("transaction", transaction["id"], "", "")
        groups[key].append(transaction)
    return [
        build_fill(group)
        for _, group in sorted(
            groups.items(),
            key=lambda item: (item[1][0].get("timestamp", ""), item[0]),
        )
    ]


def normalize_lifecycle_events(transactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events = []
    for transaction in transactions:
        event_type = lifecycle_event_type(transaction)
        if not event_type:
            continue
        parsed = parse_osi_parts(transaction.get("symbol", ""))
        events.append(
            {
                "event_id": transaction.get("id"),
                "event_type": event_type,
                "occurred_at": transaction.get("timestamp"),
                "ticker": parsed.get("underlying") or transaction.get("symbol"),
                "symbol": transaction.get("symbol"),
                "expiration": parsed.get("expiration"),
                "option_type": parsed.get("option_type"),
                "strike": parsed.get("strike"),
                "quantity": abs(number(transaction.get("quantity"))),
                "description": transaction.get("description"),
                "transaction_type": transaction.get("type"),
                "sub_type": transaction.get("sub_type"),
            }
        )
    return events


def read_cursor(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"last_timestamp": None, "seen_transaction_ids": []}
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"last_timestamp": None, "seen_transaction_ids": []}
    return value if isinstance(value, dict) else {"last_timestamp": None, "seen_transaction_ids": []}


def request_start(
    cursor: dict[str, Any],
    explicit_start: datetime | None,
    overlap_minutes: int,
    full_refresh: bool,
) -> datetime | None:
    if explicit_start or full_refresh:
        return explicit_start
    last_timestamp = parse_timestamp(cursor.get("last_timestamp"))
    return last_timestamp - timedelta(minutes=max(overlap_minutes, 0)) if last_timestamp else None


def fetch_history(
    client: Any,
    request_factory: Callable[..., Any],
    start: datetime | None = None,
    end: datetime | None = None,
    page_size: int = 100,
    max_pages: int = 100,
) -> tuple[list[dict[str, Any]], int]:
    transactions: list[dict[str, Any]] = []
    next_token = None
    pages = 0
    while pages < max_pages:
        request = request_factory(
            start=start,
            end=end,
            page_size=page_size,
            next_token=next_token,
        )
        page = client.get_history(request)
        pages += 1
        transactions.extend(
            normalize_transaction(transaction)
            for transaction in (field(page, "transactions", []) or [])
        )
        next_token = field(page, "next_token")
        if not next_token:
            break
    if next_token:
        raise RuntimeError(f"Public history exceeded max_pages={max_pages}")
    return transactions, pages


def build_snapshot(
    client: Any,
    request_factory: Callable[..., Any],
    cursor: dict[str, Any] | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    page_size: int = 100,
    max_pages: int = 100,
    overlap_minutes: int = 15,
    full_refresh: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    cursor = cursor or {"last_timestamp": None, "seen_transaction_ids": []}
    effective_start = request_start(cursor, start, overlap_minutes, full_refresh)
    transactions, pages = fetch_history(
        client,
        request_factory,
        start=effective_start,
        end=end,
        page_size=page_size,
        max_pages=max_pages,
    )
    seen = set() if full_refresh else set(cursor.get("seen_transaction_ids", []))
    unique_by_id = {
        transaction["id"]: transaction
        for transaction in transactions
        if transaction.get("id") and transaction["id"] not in seen
    }
    new_transactions = sorted(
        unique_by_id.values(),
        key=lambda transaction: (transaction.get("timestamp", ""), transaction["id"]),
    )
    portfolio = normalize_portfolio(client.get_portfolio())
    timestamps = [
        parse_timestamp(transaction.get("timestamp"))
        for transaction in transactions
        if parse_timestamp(transaction.get("timestamp"))
    ]
    previous_timestamp = parse_timestamp(cursor.get("last_timestamp"))
    latest_timestamp = max([*timestamps, previous_timestamp] if previous_timestamp else timestamps, default=None)
    all_seen = list(dict.fromkeys([*cursor.get("seen_transaction_ids", []), *unique_by_id.keys()]))[-5000:]
    new_cursor = {
        "last_timestamp": latest_timestamp.isoformat() if latest_timestamp else cursor.get("last_timestamp"),
        "seen_transaction_ids": all_seen,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    snapshot = {
        "source": "public_api",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "history": {
            "request_start": effective_start.isoformat() if effective_start else None,
            "request_end": end.isoformat() if end else None,
            "pages": pages,
            "transactions_seen": len(transactions),
            "new_transactions": len(new_transactions),
            "new_trade_transactions": sum(1 for item in new_transactions if item.get("type") == "TRADE"),
        },
        **portfolio,
        "fills": normalize_fills(new_transactions),
        "lifecycle_events": normalize_lifecycle_events(new_transactions),
    }
    return snapshot, new_cursor


def parse_cli_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = parse_timestamp(value)
    if not parsed:
        raise argparse.ArgumentTypeError(f"Invalid ISO timestamp: {value}")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cursor", default=str(DEFAULT_CURSOR))
    parser.add_argument("--output", default=str(DEFAULT_SNAPSHOT))
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--overlap-minutes", type=int, default=15)
    parser.add_argument("--full-refresh", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    try:
        from public_api_sdk import HistoryRequest
    except ImportError:
        from common import configure_public_imports

        configure_public_imports()
        try:
            from public_api_sdk import HistoryRequest
        except ImportError as exc:
            raise SystemExit(
                "Public.com SDK is unavailable. Set PUBLIC_IMPORTS_DIR to the helper scripts directory."
            ) from exc

    cursor_path = Path(args.cursor)
    output_path = Path(args.output)
    snapshot, cursor = build_snapshot(
        get_public_client(),
        HistoryRequest,
        cursor=read_cursor(cursor_path),
        start=parse_cli_timestamp(args.start),
        end=parse_cli_timestamp(args.end),
        page_size=args.page_size,
        max_pages=args.max_pages,
        overlap_minutes=args.overlap_minutes,
        full_refresh=args.full_refresh,
    )
    atomic_write_json(output_path, snapshot)
    atomic_write_json(cursor_path, cursor)
    if args.json:
        print(json.dumps(snapshot, indent=2, default=str))
    else:
        history = snapshot["history"]
        print(
            f"Public snapshot: {len(snapshot['positions'])} positions, "
            f"{len(snapshot['fills'])} fills from {history['new_trade_transactions']} new trade transactions"
        )
        print(f"  snapshot: {output_path}")
        print(f"  cursor: {cursor_path}")


if __name__ == "__main__":
    main()
