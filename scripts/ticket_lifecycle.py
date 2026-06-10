#!/usr/bin/env python3.12
"""Inspect and manually close durable execution-ticket lifecycle records."""
from __future__ import annotations

import argparse
import json

from storage import (
    ACTIVE_TICKET_STATUSES,
    DEFAULT_DB_FILE,
    TICKET_LIFECYCLE_STATUSES,
    connect,
    list_tickets,
    set_ticket_lifecycle,
    ticket_lifecycle_counts,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DEFAULT_DB_FILE))
    parser.add_argument("--status", nargs="+", choices=TICKET_LIFECYCLE_STATUSES)
    parser.add_argument("--active", action="store_true")
    parser.add_argument("--ticket-id")
    parser.add_argument("--set-status", choices=("PENDING", "CANCELLED", "EXPIRED"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if bool(args.ticket_id) != bool(args.set_status):
        raise SystemExit("--ticket-id and --set-status must be provided together")

    con = connect(args.db)
    try:
        changed = None
        if args.ticket_id:
            changed = set_ticket_lifecycle(con, args.ticket_id, args.set_status)
            if not changed:
                raise SystemExit(f"Unknown ticket_id: {args.ticket_id}")
        statuses = list(ACTIVE_TICKET_STATUSES) if args.active else args.status
        tickets = list_tickets(con, statuses=statuses)
        result = {
            "database": args.db,
            "updated": changed,
            "counts": ticket_lifecycle_counts(con),
            "tickets": tickets,
        }
    finally:
        con.close()

    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return
    print(f"Ticket lifecycle: {result['counts']}")
    for ticket in result["tickets"]:
        print(
            f"  {ticket['ticket_id']} {ticket['lifecycle_status']:<10} "
            f"{ticket.get('ticker')} {ticket.get('strategy')} "
            f"{ticket.get('filled_quantity', 0):g}/{ticket.get('target_quantity', 1):g}"
        )


if __name__ == "__main__":
    main()
