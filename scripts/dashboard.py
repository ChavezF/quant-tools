#!/usr/bin/env python3.12
"""Generate a static HTML dashboard from quant-tools reports."""
from __future__ import annotations

import argparse
import html
import json
from datetime import datetime
from pathlib import Path
from typing import Any


def read_json(path: Path | None) -> dict[str, Any]:
    if not path or not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def money(value: Any) -> str:
    try:
        return f"${float(value):,.0f}"
    except (TypeError, ValueError):
        return "-"


def rel_link(base: Path, path: str | None) -> str:
    if not path:
        return ""
    target = Path(path)
    try:
        return esc(str(target.relative_to(base)))
    except ValueError:
        return esc(str(target))


def render_actions(plan: dict[str, Any]) -> str:
    rows = plan.get("actions", [])
    body = []
    for row in rows[:30]:
        if row.get("action_decision") == "REJECT":
            continue
        candidate = row.get("candidate", {})
        execution = candidate.get("execution", {})
        failed = ", ".join(check["name"] for check in row.get("checks", []) if not check.get("ok")) or "none"
        detail = (
            f"<details><summary>View</summary>"
            f"<div><b>Profile:</b> {esc(row.get('profile_note'))}</div>"
            f"<div><b>Sizing:</b> {esc(row.get('adaptive_sizing', {}).get('note'))}</div>"
            f"<div><b>Correlation:</b> {esc(row.get('correlation', {}).get('note'))}</div>"
            f"<div><b>Failed checks:</b> {esc(failed)}</div></details>"
        )
        body.append(
            "<tr>"
            f"<td><span class='pill {esc(row.get('action_decision', '').lower())}'>{esc(row.get('action_decision'))}</span></td>"
            f"<td>{esc(row.get('ticker'))}</td>"
            f"<td>{esc(row.get('strategy'))}</td>"
            f"<td>{float(row.get('score') or 0):.1f}</td>"
            f"<td>{float(row.get('action_size_multiplier') or 0):.2f}</td>"
            f"<td>{esc(execution.get('suggested_limit_credit'))}</td>"
            f"<td>{esc(execution.get('do_not_chase_below'))}</td>"
            f"<td>{esc(execution.get('execution_grade'))}</td>"
            f"<td>{esc(row.get('profile_signal'))}</td>"
            f"<td>{detail}</td>"
            "</tr>"
        )
    if not body:
        body.append("<tr><td colspan='10'>No actionable candidates.</td></tr>")
    return "\n".join(body)


def render_alerts(alerts: dict[str, Any]) -> str:
    rows = []
    for row in alerts.get("alerts", [])[:25]:
        rows.append(
            "<tr>"
            f"<td><span class='priority {esc(row.get('priority', '').lower())}'>{esc(row.get('priority'))}</span></td>"
            f"<td>{esc(row.get('kind'))}</td>"
            f"<td>{esc(row.get('title'))}</td>"
            f"<td>{esc(row.get('detail'))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='4'>No alerts.</td></tr>")
    return "\n".join(rows)


def render_tickets(tickets: dict[str, Any]) -> str:
    rows = []
    for row in tickets.get("tickets", [])[:25]:
        rows.append(
            "<tr>"
            f"<td>{esc(row.get('decision'))}</td>"
            f"<td>{esc(row.get('ticker'))}</td>"
            f"<td>{esc(row.get('strategy'))}</td>"
            f"<td>{esc(row.get('expiration'))}</td>"
            f"<td>{esc(row.get('strikes'))}</td>"
            f"<td>{esc(row.get('order_action'))}</td>"
            f"<td>{esc(row.get('limit_credit'))}</td>"
            f"<td>{esc(row.get('do_not_chase_below'))}</td>"
            f"<td>{money(row.get('max_loss'))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='9'>No tickets.</td></tr>")
    return "\n".join(rows)


def render_score_bands(analytics: dict[str, Any]) -> str:
    rows = []
    for band, row in analytics.get("by_score_band", {}).items():
        rows.append(
            "<tr>"
            f"<td>{esc(band)}</td>"
            f"<td>{esc(row.get('count'))}</td>"
            f"<td>{float(row.get('win_rate') or 0):.1f}%</td>"
            f"<td>{money(row.get('expectancy'))}</td>"
            f"<td>{float(row.get('avg_return_on_risk_pct') or 0):.2f}%</td>"
            f"<td>{esc(row.get('profit_factor'))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='6'>No closed-trade history.</td></tr>")
    return "\n".join(rows)


def render_strategy_feedback(feedback: dict[str, Any]) -> str:
    rows = []
    for strategy, row in feedback.get("strategy_adjustments", {}).items():
        rows.append(
            "<tr>"
            f"<td>{esc(strategy)}</td>"
            f"<td>{esc(row.get('signal'))}</td>"
            f"<td>{float(row.get('multiplier') or 0):.2f}</td>"
            f"<td>{esc(row.get('sample_size'))}</td>"
            f"<td>{money(row.get('expectancy'))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='5'>No calibrated strategy history.</td></tr>")
    return "\n".join(rows)


def render_links(manifest: dict[str, Any], base: Path) -> str:
    reports = manifest.get("reports", {})
    links = []
    for name, path in reports.items():
        if path:
            links.append(f"<a href='{rel_link(base, path)}'>{esc(name)}</a>")
    manifest_path = base / "manifest.json"
    if manifest_path.exists():
        links.append("<a href='manifest.json'>manifest</a>")
    return " ".join(links)


def build_dashboard(
    *,
    plan: dict[str, Any],
    alerts: dict[str, Any],
    tickets: dict[str, Any],
    manifest: dict[str, Any],
    base: Path,
    analytics: dict[str, Any] | None = None,
    feedback: dict[str, Any] | None = None,
) -> str:
    analytics = analytics or plan.get("historical_analytics", {})
    feedback = feedback or {}
    plan_summary = plan.get("summary", {})
    alert_summary = alerts.get("summary", {})
    overall = analytics.get("overall", {})
    drawdown = analytics.get("drawdown", {})
    ticket_count = len(tickets.get("tickets", []))
    as_of = manifest.get("created_at") or datetime.now().isoformat()
    links = render_links(manifest, base)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Quant Tools Dashboard</title>
  <style>
    :root {{ color-scheme: light; --ink:#18202f; --muted:#667085; --line:#d9dee8; --bg:#f7f9fc; --panel:#ffffff; --ok:#0b7a53; --warn:#9a5b00; --bad:#b42318; }}
    body {{ margin:0; font-family: Segoe UI, Arial, sans-serif; background:var(--bg); color:var(--ink); }}
    header {{ padding:24px 32px 12px; border-bottom:1px solid var(--line); background:var(--panel); }}
    h1 {{ margin:0 0 6px; font-size:24px; font-weight:650; }}
    h2 {{ margin:24px 0 10px; font-size:17px; }}
    main {{ padding:18px 32px 36px; }}
    .muted {{ color:var(--muted); }}
    .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:12px; margin-top:14px; }}
    .card {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px; }}
    .card b {{ display:block; font-size:24px; margin-top:6px; }}
    table {{ width:100%; border-collapse:collapse; background:var(--panel); border:1px solid var(--line); border-radius:8px; overflow:hidden; }}
    th,td {{ padding:9px 10px; border-bottom:1px solid var(--line); text-align:left; font-size:13px; vertical-align:top; }}
    th {{ background:#eef2f7; font-weight:650; }}
    th[data-sort] {{ cursor:pointer; user-select:none; }}
    tr:last-child td {{ border-bottom:0; }}
    .pill,.priority {{ display:inline-block; padding:2px 7px; border-radius:999px; font-size:12px; font-weight:650; }}
    .approve,.high {{ background:#e8f5ef; color:var(--ok); }}
    .reduce,.medium {{ background:#fff4dc; color:var(--warn); }}
    .reject,.low {{ background:#fdecec; color:var(--bad); }}
    .links a {{ display:inline-block; margin-right:10px; color:#175cd3; text-decoration:none; }}
    details summary {{ color:#175cd3; cursor:pointer; }}
    details div {{ margin-top:5px; max-width:420px; color:var(--muted); }}
    .split {{ display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr); gap:16px; }}
    @media (max-width:900px) {{ header,main {{ padding-left:16px; padding-right:16px; }} .split {{ grid-template-columns:1fr; }} .table-wrap {{ overflow-x:auto; }} }}
  </style>
</head>
<body>
  <header>
    <h1>Quant Tools Dashboard</h1>
    <div class="muted">Generated {esc(as_of)}</div>
    <div class="links">{links}</div>
  </header>
  <main>
    <section class="cards">
      <div class="card">Approved<b>{esc(plan_summary.get('approve', 0))}</b></div>
      <div class="card">Reduced<b>{esc(plan_summary.get('reduce', 0))}</b></div>
      <div class="card">Rejected<b>{esc(plan_summary.get('reject', 0))}</b></div>
      <div class="card">High Alerts<b>{esc(alert_summary.get('high', 0))}</b></div>
      <div class="card">Tickets<b>{ticket_count}</b></div>
      <div class="card">Expectancy<b>{money(overall.get('expectancy'))}</b></div>
      <div class="card">Max Drawdown<b>{money(drawdown.get('max_drawdown'))}</b></div>
      <div class="card">Min Score<b>{float(feedback.get('recommended_min_score') or 0):.0f}</b></div>
    </section>
    <h2>Action Plan</h2>
    <div class="table-wrap"><table class="sortable">
      <thead><tr><th data-sort>Action</th><th data-sort>Ticker</th><th data-sort>Strategy</th><th data-sort>Score</th><th data-sort>Size</th><th data-sort>Limit</th><th data-sort>Floor</th><th data-sort>Exec</th><th data-sort>Profile</th><th>Rationale</th></tr></thead>
      <tbody>{render_actions(plan)}</tbody>
    </table></div>
    <div class="split">
      <section>
        <h2>Score-Band Performance</h2>
        <div class="table-wrap"><table class="sortable">
          <thead><tr><th data-sort>Score</th><th data-sort>N</th><th data-sort>Win</th><th data-sort>Expectancy</th><th data-sort>Return/Risk</th><th data-sort>PF</th></tr></thead>
          <tbody>{render_score_bands(analytics)}</tbody>
        </table></div>
      </section>
      <section>
        <h2>Strategy Calibration</h2>
        <div class="table-wrap"><table class="sortable">
          <thead><tr><th data-sort>Strategy</th><th data-sort>Signal</th><th data-sort>Multiplier</th><th data-sort>N</th><th data-sort>Expectancy</th></tr></thead>
          <tbody>{render_strategy_feedback(feedback)}</tbody>
        </table></div>
      </section>
    </div>
    <h2>Alerts</h2>
    <div class="table-wrap"><table class="sortable">
      <thead><tr><th>Priority</th><th>Kind</th><th>Title</th><th>Detail</th></tr></thead>
      <tbody>{render_alerts(alerts)}</tbody>
    </table></div>
    <h2>Execution Tickets</h2>
    <div class="table-wrap"><table class="sortable">
      <thead><tr><th>Decision</th><th>Ticker</th><th>Strategy</th><th>Expiration</th><th>Strikes</th><th>Order</th><th>Limit</th><th>Floor</th><th>Max Loss</th></tr></thead>
      <tbody>{render_tickets(tickets)}</tbody>
    </table></div>
  </main>
  <script>
    document.querySelectorAll('th[data-sort]').forEach((header) => {{
      header.addEventListener('click', () => {{
        const table = header.closest('table');
        const body = table.tBodies[0];
        const index = Array.from(header.parentNode.children).indexOf(header);
        const ascending = header.dataset.direction !== 'asc';
        const rows = Array.from(body.rows);
        rows.sort((a, b) => {{
          const left = a.cells[index].innerText.trim().replace(/[$,%]/g, '');
          const right = b.cells[index].innerText.trim().replace(/[$,%]/g, '');
          const leftNumber = Number(left);
          const rightNumber = Number(right);
          const result = Number.isNaN(leftNumber) || Number.isNaN(rightNumber)
            ? left.localeCompare(right)
            : leftNumber - rightNumber;
          return ascending ? result : -result;
        }});
        rows.forEach((row) => body.appendChild(row));
        header.dataset.direction = ascending ? 'asc' : 'desc';
      }});
    }});
  </script>
</body>
</html>
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-dir", help="Daily workflow report directory")
    ap.add_argument("--plan")
    ap.add_argument("--alerts")
    ap.add_argument("--tickets")
    ap.add_argument("--manifest")
    ap.add_argument("--analytics")
    ap.add_argument("--feedback")
    ap.add_argument("--output")
    args = ap.parse_args()

    base = Path(args.report_dir) if args.report_dir else Path.cwd()
    plan_path = Path(args.plan) if args.plan else base / "plan.json"
    alerts_path = Path(args.alerts) if args.alerts else base / "alerts.json"
    tickets_path = Path(args.tickets) if args.tickets else base / "tickets.json"
    manifest_path = Path(args.manifest) if args.manifest else base / "manifest.json"
    analytics_path = Path(args.analytics) if args.analytics else base / "analytics.json"
    feedback_path = Path(args.feedback) if args.feedback else base / "feedback.json"
    output_path = Path(args.output) if args.output else base / "dashboard.html"

    html_out = build_dashboard(
        plan=read_json(plan_path),
        alerts=read_json(alerts_path),
        tickets=read_json(tickets_path),
        manifest=read_json(manifest_path),
        base=base,
        analytics=read_json(analytics_path),
        feedback=read_json(feedback_path),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html_out)
    print(output_path)


if __name__ == "__main__":
    main()
