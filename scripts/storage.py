#!/usr/bin/env python3.12
"""SQLite persistence helpers for quant-tools state and reconciliation data."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from common import STATE_DIR


DEFAULT_DB_FILE = STATE_DIR / "quant_tools.db"
SCHEMA_VERSION = 1


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
    if version >= SCHEMA_VERSION:
        return
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
                _json(ticket),
                ts,
            )
        )
    con.executemany(
        """
        INSERT INTO tickets(ticket_id, ticker, strategy, decision, expiration, payload_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticket_id) DO UPDATE SET
            ticker=excluded.ticker,
            strategy=excluded.strategy,
            decision=excluded.decision,
            expiration=excluded.expiration,
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


def upsert_fills(con: sqlite3.Connection, fills: list[dict[str, Any]]) -> int:
    rows = []
    for idx, fill in enumerate(fills):
        fill_id = fill.get("fill_id") or fill.get("id") or f"{fill.get('filled_at', '')}:{fill.get('symbol', '')}:{idx}"
        rows.append(
            (
                str(fill_id),
                fill.get("ticket_id"),
                fill.get("symbol"),
                fill.get("ticker"),
                fill.get("strategy"),
                fill.get("side"),
                float(fill.get("quantity") or 0),
                float(fill.get("price") or fill.get("credit") or 0),
                fill.get("filled_at"),
                _json(fill),
            )
        )
    con.executemany(
        """
        INSERT INTO broker_fills(fill_id, ticket_id, symbol, ticker, strategy, side, quantity, price, filled_at, payload_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(fill_id) DO UPDATE SET
            ticket_id=excluded.ticket_id,
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
