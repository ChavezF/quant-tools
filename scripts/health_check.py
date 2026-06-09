#!/usr/bin/env python3.12
"""Run dependency-light repository and runtime health checks."""
from __future__ import annotations

import argparse
import compileall
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from common import PROJECT_ROOT
from database_maintenance import integrity_report
from storage import DEFAULT_DB_FILE


def check_config(path: Path) -> dict[str, Any]:
    try:
        json.loads(path.read_text())
        return {"name": "config_json", "ok": True, "detail": str(path)}
    except (OSError, json.JSONDecodeError) as exc:
        return {"name": "config_json", "ok": False, "detail": str(exc)}


def check_compile(root: Path) -> dict[str, Any]:
    ok = compileall.compile_dir(root / "scripts", quiet=1) and compileall.compile_dir(root / "tests", quiet=1)
    return {"name": "compile", "ok": bool(ok), "detail": "scripts and tests"}


def check_tests(root: Path) -> dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=120,
    )
    detail = (proc.stdout + proc.stderr).strip().splitlines()
    return {
        "name": "unit_tests",
        "ok": proc.returncode == 0,
        "detail": detail[-1] if detail else f"returncode={proc.returncode}",
    }


def build_health_report(
    root: Path = PROJECT_ROOT,
    *,
    db_path: str | Path = DEFAULT_DB_FILE,
    include_tests: bool = True,
    include_db: bool = True,
) -> dict[str, Any]:
    checks = [
        check_config(root / "config.example.json"),
        check_compile(root),
    ]
    if include_tests:
        checks.append(check_tests(root))
    if include_db:
        db = integrity_report(db_path)
        checks.append({"name": "database_integrity", "ok": bool(db["ok"]), "detail": db})
    return {
        "ok": all(check["ok"] for check in checks),
        "checks": checks,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB_FILE))
    ap.add_argument("--skip-tests", action="store_true")
    ap.add_argument("--skip-db", action="store_true")
    ap.add_argument("--output")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    report = build_health_report(
        db_path=args.db,
        include_tests=not args.skip_tests,
        include_db=not args.skip_db,
    )
    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=2, default=str))
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        for check in report["checks"]:
            print(f"{'OK' if check['ok'] else 'FAIL':<4} {check['name']}: {check['detail']}")
    raise SystemExit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
