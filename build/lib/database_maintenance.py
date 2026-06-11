#!/usr/bin/env python3.12
"""SQLite integrity, backup, retention, and optional vacuum operations."""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from storage import DEFAULT_DB_FILE, connect, table_counts


def integrity_report(db_path: str | Path) -> dict[str, Any]:
    path = Path(db_path)
    if not path.exists():
        return {"exists": False, "ok": True, "message": "database not created yet", "path": str(path)}
    con = connect(path)
    try:
        result = str(con.execute("PRAGMA quick_check").fetchone()[0])
        version = int(con.execute("PRAGMA user_version").fetchone()[0])
        return {
            "exists": True,
            "ok": result.lower() == "ok",
            "message": result,
            "path": str(path),
            "schema_version": version,
            "counts": table_counts(con),
            "size_bytes": path.stat().st_size,
        }
    finally:
        con.close()


def backup_database(db_path: str | Path, backup_dir: str | Path) -> Path | None:
    source_path = Path(db_path)
    if not source_path.exists():
        return None
    target_dir = Path(backup_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"quant-tools-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.db"
    source = sqlite3.connect(source_path)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return target


def prune_backups(
    backup_dir: str | Path,
    *,
    retention_days: int = 30,
    keep_last: int = 14,
    now: datetime | None = None,
) -> list[str]:
    path = Path(backup_dir)
    if not path.exists():
        return []
    files = sorted(path.glob("quant-tools-*.db"), key=lambda item: item.stat().st_mtime, reverse=True)
    cutoff = (now or datetime.now()) - timedelta(days=retention_days)
    deleted = []
    for index, file_path in enumerate(files):
        modified = datetime.fromtimestamp(file_path.stat().st_mtime)
        if index >= keep_last and modified < cutoff:
            file_path.unlink()
            deleted.append(str(file_path))
    return deleted


def maintain_database(
    db_path: str | Path,
    backup_dir: str | Path,
    *,
    create_backup: bool = True,
    retention_days: int = 30,
    keep_last: int = 14,
    vacuum: bool = False,
) -> dict[str, Any]:
    before = integrity_report(db_path)
    backup = backup_database(db_path, backup_dir) if create_backup and before["exists"] else None
    deleted = prune_backups(backup_dir, retention_days=retention_days, keep_last=keep_last)
    vacuumed = False
    if vacuum and before["exists"] and before["ok"]:
        con = connect(db_path)
        try:
            con.execute("VACUUM")
            vacuumed = True
        finally:
            con.close()
    after = integrity_report(db_path)
    return {
        "ok": bool(before["ok"] and after["ok"]),
        "integrity_before": before,
        "integrity_after": after,
        "backup": str(backup) if backup else None,
        "deleted_backups": deleted,
        "vacuumed": vacuumed,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB_FILE))
    ap.add_argument("--backup-dir")
    ap.add_argument("--retention-days", type=int, default=30)
    ap.add_argument("--keep-last", type=int, default=14)
    ap.add_argument("--no-backup", action="store_true")
    ap.add_argument("--vacuum", action="store_true")
    ap.add_argument("--output")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    backup_dir = args.backup_dir or str(Path(args.db).parent / "backups")
    report = maintain_database(
        args.db,
        backup_dir,
        create_backup=not args.no_backup,
        retention_days=args.retention_days,
        keep_last=args.keep_last,
        vacuum=args.vacuum,
    )
    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=2, default=str))
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(f"Database integrity: {'OK' if report['ok'] else 'FAILED'}")
        print(f"Backup: {report['backup'] or 'not created'}")
        print(f"Deleted backups: {len(report['deleted_backups'])}")
    raise SystemExit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
