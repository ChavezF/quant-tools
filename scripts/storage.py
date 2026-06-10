#!/usr/bin/env python3.12
"""SQLite persistence helpers for quant-tools state and reconciliation data."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common import STATE_DIR


DEFAULT_DB_FILE = STATE_DIR / "quant_tools.db"
SCHEMA_VERSION = 2


def now_iso() -> str:
    return datetime.now().isoformat()


def connect(db_path: str | Path = DEFAULT_DB_FILE) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    migrate(con)
    return con


def migrate(con: sqlite3.Connection) -> None:
    version = int(con.execute("PRAGMA user_version").fetchone()[0])
    if version < 1:
        con.executescript(
            """
        CREATE TABLE IF NOT EXISTS trades (
            id TEXT PRIMARY KEY,
            ticket_id TEXT,
            status TEXT,
            ticker TEXT,
            strategy TEXT,
            opened_at TEXT,
            closed_at TEXT,
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_trades_ticket ON trades(ticket_id);
        CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
        CREATE INDEX IF NOT EXISTS idx_trades_ticker_strategy ON trades(ticker, strategy);

        CREATE TABLE IF NOT EXISTS tickets (
            ticket_id TEXT PRIMARY KEY,
            ticker TEXT,
            strategy TEXT,
            decision TEXT,
            expiration TEXT,
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tickets_ticker_strategy ON tickets(ticker, strategy);

        CREATE TABLE IF NOT EXISTS broker_positions (
            snapshot_at TEXT NOT NULL,
            symbol TEXT NOT NULL,
            type TEXT,
            quantity REAL,
            current_value REAL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (snapshot_at, symbol)
        );

        CREATE TABLE IF NOT EXISTS broker_fills (
            fill_id TEXT PRIMARY KEY,
            ticket_id TEXT,
            symbol TEXT,
            ticker TEXT,
            strategy TEXT,
            side TEXT,
            quantity REAL,
            price REAL,
            filled_at TEXT,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_fills_ticket ON broker_fills(ticket_id);
        CREATE INDEX IF NOT EXISTS idx_fills_ticker_strategy ON broker_fills(ticker, strategy);

        CREATE TABLE IF NOT EXISTS reconciliation_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        PRAGMA user_version = 1;
        """
        )
        version = 1
    if version < 2:
        con.executescript(
            """
            ALTER TABLE tickets ADD COLUMN lifecycle_status TEXT NOT NULL DEFAULT 'PENDING';
            ALTER TABLE tickets ADD COLUMN target_quantity REAL NOT NULL DEFAULT 1;
            ALTER TABLE tickets ADD COLUMN filled_quantity REAL NOT NULL DEFAULT 0;
            ALTER TABLE tickets ADD COLUMN issued_at TEXT;
            ALTER TABLE tickets ADD COLUMN last_fill_at TEXT;
            CREATE INDEX IF NOT EXISTS idx_tickets_lifecycle ON tickets(lifecycle_status);
            PRAGMA user_version = 2;
            """
        )
    con.commit()


def _json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, default=str)


def upsert_trades(con: sqlite3.Connection, trades: list[dict[str, Any]]) -> int:
    rows = []
    ts = now_iso()
    for trade in trades:
        if not trade.get("id"):
            continue
        rows.append(
            (
                str(trade.get("id")),
                trade.get("ticket_id"),
                trade.get("status"),
                trade.get("ticker"),
                trade.get("strategy"),
                trade.get("opened_at"),
                trade.get("closed_at"),
                _json(trade),
                ts,
            )
        )
    con.executemany(
        """
        INSERT INTO trades(id, ticket_id, status, ticker, strategy, opened_at, closed_at, payload_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            ticket_id=excluded.ticket_id,
            status=excluded.status,
            ticker=excluded.ticker,
            strategy=excluded.strategy,
            opened_at=excluded.opened_at,
            closed_at=excluded.closed_at,
            payload_json=excluded.payload_json,
            updated_at=excluded.updated_at
        """,
        rows,
    )
    con.commit()
    return len(rows)


def upsert_tickets(con: sqlite3.Connection, tickets: list[dict[str, Any]]) -> int:
    rows = []
    ts = now_iso()
    for ticket in tickets:
        if not ticket.get("ticket_id"):
            continue
        rows.append(
            (
                str(ticket.get("ticket_id")),
                ticket.get("ticker"),
                ticket.get("strategy"),
                ticket.get("decision"),
                ticket.get("expiration"),
                ticket.get("lifecycle_status") or "PENDING",
                float(ticket.get("target_quantity") or 1),
                float(ticket.get("filled_quantity") or 0),
                ticket.get("issued_at"),
                _json(ticket),
                ts,
            )
        )
    con.executemany(
        """
        INSERT INTO tickets(
            ticket_id, ticker, strategy, decision, expiration, lifecycle_status,
            target_quantity, filled_quantity, issued_at, payload_json, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticket_id) DO UPDATE SET
            ticker=excluded.ticker,
            strategy=excluded.strategy,
            decision=excluded.decision,
            expiration=excluded.expiration,
            target_quantity=excluded.target_quantity,
            issued_at=COALESCE(tickets.issued_at, excluded.issued_at),
            payload_json=excluded.payload_json,
            updated_at=excluded.updated_at
        """,
        rows,
    )
    con.commit()
    return len(rows)


def insert_position_snapshot(
    con: sqlite3.Connection,
    positions: list[dict[str, Any]],
    snapshot_at: str | None = None,
) -> int:
    ts = snapshot_at or now_iso()
    rows = [
        (
            ts,
            str(pos.get("symbol") or pos.get("ticker") or ""),
            pos.get("type"),
            float(pos.get("quantity") or 0),
            float(pos.get("current_value") or 0),
            _json(pos),
        )
        for pos in positions
        if pos.get("symbol") or pos.get("ticker")
    ]
    con.executemany(
        """
        INSERT OR REPLACE INTO broker_positions(snapshot_at, symbol, type, quantity, current_value, payload_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    con.commit()
    return len(rows)


def fill_identity(fill: dict[str, Any], index: int = 0) -> str:
    return str(
        fill.get("fill_id")
        or fill.get("id")
        or f"{fill.get('filled_at', '')}:{fill.get('symbol', '')}:{index}"
    )


def upsert_fills(con: sqlite3.Connection, fills: list[dict[str, Any]]) -> int:
    rows = []
    for idx, fill in enumerate(fills):
        fill_id = fill_identity(fill, idx)
        rows.append(
            (
                str(fill_id),
                fill.get("ticket_id"),
                fill.get("symbol"),
                fill.get("ticker"),
                fill.get("strategy"),
                fill.get("side"),
                float(fill.get("quantity") or 0),
                float(fill.get("net_credit") or fill.get("price") or fill.get("credit") or 0),
                fill.get("filled_at"),
                _json(fill),
            )
        )
    con.executemany(
        """
        INSERT INTO broker_fills(fill_id, ticket_id, symbol, ticker, strategy, side, quantity, price, filled_at, payload_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(fill_id) DO UPDATE SET
            ticket_id=COALESCE(excluded.ticket_id, broker_fills.ticket_id),
            symbol=excluded.symbol,
            ticker=excluded.ticker,
            strategy=excluded.strategy,
            side=excluded.side,
            quantity=excluded.quantity,
            price=excluded.price,
            filled_at=excluded.filled_at,
            payload_json=excluded.payload_json
        """,
        rows,
    )
    con.commit()
    return len(rows)


ACTIVE_TICKET_STATUSES = ("PENDING", "PARTIAL")
TICKET_LIFECYCLE_STATUSES = ("PENDING", "PARTIAL", "FILLED", "OVERFILLED", "CANCELLED", "EXPIRED")


def _ticket_from_row(row: sqlite3.Row) -> dict[str, Any]:
    ticket = json.loads(row["payload_json"])
    ticket.update(
        {
            "ticket_id": row["ticket_id"],
            "lifecycle_status": row["lifecycle_status"],
            "target_quantity": float(row["target_quantity"] or 1),
            "filled_quantity": float(row["filled_quantity"] or 0),
            "issued_at": row["issued_at"],
            "last_fill_at": row["last_fill_at"],
            "updated_at": row["updated_at"],
        }
    )
    return ticket


def load_active_tickets(con: sqlite3.Connection) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in ACTIVE_TICKET_STATUSES)
    rows = con.execute(
        f"""
        SELECT * FROM tickets
        WHERE lifecycle_status IN ({placeholders})
        ORDER BY COALESCE(issued_at, updated_at), ticket_id
        """,
        ACTIVE_TICKET_STATUSES,
    ).fetchall()
    return [_ticket_from_row(row) for row in rows]


def list_tickets(
    con: sqlite3.Connection,
    statuses: list[str] | None = None,
) -> list[dict[str, Any]]:
    normalized = [str(status).upper() for status in (statuses or [])]
    params: list[str] = []
    where = ""
    if normalized:
        where = f"WHERE lifecycle_status IN ({','.join('?' for _ in normalized)})"
        params.extend(normalized)
    rows = con.execute(
        f"""
        SELECT * FROM tickets
        {where}
        ORDER BY COALESCE(issued_at, updated_at) DESC, ticket_id
        """,
        params,
    ).fetchall()
    return [_ticket_from_row(row) for row in rows]


def set_ticket_lifecycle(
    con: sqlite3.Connection,
    ticket_id: str,
    status: str,
) -> bool:
    normalized = str(status).upper()
    if normalized not in TICKET_LIFECYCLE_STATUSES:
        raise ValueError(f"Unsupported ticket lifecycle status: {status}")
    cur = con.execute(
        "UPDATE tickets SET lifecycle_status=?, updated_at=? WHERE ticket_id=?",
        (normalized, now_iso(), str(ticket_id)),
    )
    con.commit()
    return cur.rowcount > 0


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def ticket_age_hours(ticket: dict[str, Any], now: datetime | None = None) -> float | None:
    reference = parse_datetime(ticket.get("issued_at") or ticket.get("updated_at"))
    if not reference:
        return None
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return max(0.0, (current.astimezone(timezone.utc) - reference).total_seconds() / 3600)


def setup_key(ticket: dict[str, Any]) -> str:
    return "|".join(
        str(ticket.get(key) or "").strip().upper()
        for key in ("ticker", "strategy", "expiration", "strikes")
    )


def apply_lifecycle_policy(
    con: sqlite3.Connection,
    *,
    pending_expiry_hours: float = 24,
    partial_review_hours: float = 4,
    now: datetime | None = None,
) -> dict[str, Any]:
    active = load_active_tickets(con)
    expired = []
    stale_partials = []
    groups: dict[str, list[dict[str, Any]]] = {}
    for ticket in active:
        age = ticket_age_hours(ticket, now)
        ticket["age_hours"] = round(age, 2) if age is not None else None
        status = ticket.get("lifecycle_status")
        if status == "PENDING" and age is not None and age >= pending_expiry_hours:
            set_ticket_lifecycle(con, str(ticket["ticket_id"]), "EXPIRED")
            expired.append(
                {
                    "ticket_id": ticket["ticket_id"],
                    "ticker": ticket.get("ticker"),
                    "strategy": ticket.get("strategy"),
                    "age_hours": ticket["age_hours"],
                }
            )
            continue
        if status == "PARTIAL" and age is not None and age >= partial_review_hours:
            stale_partials.append(
                {
                    "ticket_id": ticket["ticket_id"],
                    "ticker": ticket.get("ticker"),
                    "strategy": ticket.get("strategy"),
                    "age_hours": ticket["age_hours"],
                    "filled_quantity": ticket.get("filled_quantity"),
                    "target_quantity": ticket.get("target_quantity"),
                }
            )
        groups.setdefault(setup_key(ticket), []).append(ticket)
    duplicate_setups = [
        {
            "setup_key": key,
            "ticket_ids": [ticket["ticket_id"] for ticket in tickets],
            "count": len(tickets),
        }
        for key, tickets in sorted(groups.items())
        if key.strip("|") and len(tickets) > 1
    ]
    return {
        "pending_expiry_hours": pending_expiry_hours,
        "partial_review_hours": partial_review_hours,
        "expired_tickets": expired,
        "stale_partial_tickets": stale_partials,
        "duplicate_active_setups": duplicate_setups,
    }


def load_fills_for_reconciliation(
    con: sqlite3.Connection,
    ticket_ids: list[str],
    new_fill_ids: list[str],
) -> list[dict[str, Any]]:
    clauses = []
    params: list[str] = []
    if ticket_ids:
        clauses.append(f"ticket_id IN ({','.join('?' for _ in ticket_ids)})")
        params.extend(ticket_ids)
    if new_fill_ids:
        clauses.append(f"fill_id IN ({','.join('?' for _ in new_fill_ids)})")
        params.extend(new_fill_ids)
    if not clauses:
        return []
    rows = con.execute(
        f"SELECT * FROM broker_fills WHERE {' OR '.join(clauses)} ORDER BY filled_at, fill_id",
        params,
    ).fetchall()
    fills = []
    for row in rows:
        fill = json.loads(row["payload_json"])
        fill.update(
            {
                "fill_id": row["fill_id"],
                "ticket_id": row["ticket_id"],
                "quantity": float(row["quantity"] or 0),
                "price": float(row["price"] or 0),
                "filled_at": row["filled_at"],
            }
        )
        fills.append(fill)
    return fills


def apply_ticket_lifecycle(con: sqlite3.Connection, ticket_matches: list[dict[str, Any]]) -> int:
    updated = 0
    ts = now_iso()
    for match in ticket_matches:
        ticket_id = match.get("ticket_id")
        if not ticket_id:
            continue
        status = str(match.get("status") or "UNMATCHED")
        lifecycle = {
            "UNMATCHED": "PENDING",
            "PARTIAL": "PARTIAL",
            "MATCHED": "FILLED",
            "OVERFILLED": "OVERFILLED",
        }.get(status, "PENDING")
        fill_ids = [str(value) for value in match.get("fill_ids", []) if value]
        last_fill_at = None
        if fill_ids:
            placeholders = ",".join("?" for _ in fill_ids)
            last_fill_at = con.execute(
                f"SELECT MAX(filled_at) FROM broker_fills WHERE fill_id IN ({placeholders})",
                fill_ids,
            ).fetchone()[0]
            con.execute(
                f"UPDATE broker_fills SET ticket_id=? WHERE fill_id IN ({placeholders})",
                [str(ticket_id), *fill_ids],
            )
        con.execute(
            """
            UPDATE tickets
            SET lifecycle_status=?, filled_quantity=?, last_fill_at=COALESCE(?, last_fill_at), updated_at=?
            WHERE ticket_id=?
            """,
            (
                lifecycle,
                float(match.get("filled_quantity") or 0),
                last_fill_at,
                ts,
                str(ticket_id),
            ),
        )
        updated += 1
    con.commit()
    return updated


def ticket_lifecycle_counts(con: sqlite3.Connection) -> dict[str, int]:
    rows = con.execute(
        "SELECT lifecycle_status, COUNT(*) AS count FROM tickets GROUP BY lifecycle_status"
    ).fetchall()
    return {str(row["lifecycle_status"]): int(row["count"]) for row in rows}


def record_reconciliation(con: sqlite3.Connection, report: dict[str, Any]) -> int:
    cur = con.execute(
        "INSERT INTO reconciliation_runs(created_at, payload_json) VALUES (?, ?)",
        (report.get("created_at") or now_iso(), _json(report)),
    )
    con.commit()
    return int(cur.lastrowid)


def table_counts(con: sqlite3.Connection) -> dict[str, int]:
    tables = ["trades", "tickets", "broker_positions", "broker_fills", "reconciliation_runs"]
    return {table: int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in tables}


def export_journal_state(con: sqlite3.Connection) -> dict[str, Any]:
    rows = con.execute("SELECT payload_json FROM trades ORDER BY opened_at, id").fetchall()
    return {"version": 1, "last_updated": now_iso(), "trades": [json.loads(row["payload_json"]) for row in rows]}
