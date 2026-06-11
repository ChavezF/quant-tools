#!/usr/bin/env python3.12
"""Generate a static HTML dashboard from quant-tools reports.

Layout answers three operator questions, in priority order:
  1. What do I do right now?   (regime/sizing banner, executable candidates,
     phantom positions, urgent management actions)
  2. What changed since yesterday?   (alerts, drift, validation, management)
  3. What's the full picture?   (analytics, scorecard, attribution, stress —
     collapsed sections)

Read-only by design: no buttons, no forms, no path to order placement.
Submission transitions stay in the CLI (quant.py stage, explicit confirm).

The executable-candidate list mirrors the 10:30 Telegram message exactly:
both call hermes_ops.categorize_executable_tickets with the iv_ranks
persisted by cron_executable_scan (iv_ranks.json). When that file is
missing or empty the IVR gate is bypassed — same as the Telegram path —
and the dashboard says so instead of silently looking more permissive.
"""
from __future__ import annotations

import argparse
import html
from datetime import datetime
from pathlib import Path
from typing import Any

from common import parse_regime_from_brief, read_json
from hermes_ops import _classify_ivr, categorize_executable_tickets


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


# Regime verdicts that pass the standing rule "half size until macro regime
# > FAVORABLE or portfolio IVRank > 50" (common.REGIME_TO_SIZING maps these
# to normal/aggressive sizing). Anything else — including an unparseable
# regime — is conservative.
FULL_SIZE_REGIMES = {"FAVORABLE", "AGGRESSIVE"}
FULL_SIZE_MODES = {"normal", "aggressive"}

CONSERVATIVE_BANNER = "Half-size until macro FAVORABLE or portfolio IVRank > 50."


def regime_context(
    brief_text: str | None,
    iv_payload: dict[str, Any],
    allocation: dict[str, Any],
) -> dict[str, Any]:
    """Resolve macro regime + sizing mode from persisted artifacts only.

    Priority: brief.out verdict (planning runs) -> iv_ranks.json context
    (executable runs, recorded by cron_executable_scan) -> allocation.json
    sizing metadata. Never re-fetches market data.
    """
    regime = parse_regime_from_brief(brief_text) if brief_text else None
    regime_source = "brief" if regime else None
    if not regime and iv_payload.get("regime"):
        regime = str(iv_payload["regime"]).upper()
        regime_source = "10:30 scan"
    sizing = (allocation.get("limits") or {}).get("sizing_mode")
    sizing_source = "allocation" if sizing else None
    if not sizing and iv_payload.get("sizing_mode"):
        sizing = str(iv_payload["sizing_mode"])
        sizing_source = "10:30 scan"
    conservative = (
        (regime or "") not in FULL_SIZE_REGIMES
        or (sizing or "cautious").lower() not in FULL_SIZE_MODES
    )
    return {
        "regime": regime,
        "regime_source": regime_source,
        "sizing": sizing,
        "sizing_source": sizing_source,
        "conservative": conservative,
    }


def render_exception_strip(
    management_summary: dict[str, Any],
    recon_summary: dict[str, Any],
) -> str:
    items = [
        ("PHANTOM positions", management_summary.get("phantom_positions")),
        ("PARTIAL broker positions", management_summary.get("partial_positions")),
        ("UNKNOWN broker positions", management_summary.get("unknown_positions")),
        ("position exceptions", recon_summary.get("position_exceptions", recon_summary.get("missing_positions"))),
        ("stale partial tickets", recon_summary.get("stale_partial_tickets")),
        ("duplicate active setups", recon_summary.get("duplicate_active_setups")),
        ("unmatched tickets", recon_summary.get("unmatched_tickets")),
    ]
    flagged = [f"{int(count)} {label}" for label, count in items if count and int(count) > 0]
    if not flagged:
        return ""
    return f"<div class='banner bad'>⚠ {esc(' · '.join(flagged))} — reconcile before staging anything.</div>"


def render_executable(categories: dict[str, list[dict[str, Any]]]) -> str:
    rows = []
    for row in categories["executable"]:
        rows.append(
            "<tr>"
            f"<td>{esc(row.get('ticker'))}</td>"
            f"<td>{esc(row.get('strategy'))}</td>"
            f"<td>{esc(row.get('expiration'))}</td>"
            f"<td>{esc(row.get('strikes'))}</td>"
            f"<td>{esc(row.get('order_action'))}</td>"
            f"<td>{esc(row.get('limit_credit'))}</td>"
            f"<td>{esc(row.get('do_not_chase_below'))}</td>"
            f"<td>{esc(row.get('target_quantity'))}</td>"
            f"<td>{float(row.get('size_multiplier') or 0):.2f}</td>"
            f"<td>{esc(row.get('score'))}</td>"
            f"<td>{money(row.get('max_loss'))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='11'>No executable candidates today.</td></tr>")
    return "\n".join(rows)


def render_held_by_ivr(
    categories: dict[str, list[dict[str, Any]]],
    iv_ranks: dict[str, float],
) -> str:
    rows = []
    for row in categories["held_by_ivr"]:
        ticker = str(row.get("ticker", "?")).upper()
        try:
            ivr: float | None = float(iv_ranks.get(ticker))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            ivr = None
        rows.append(
            "<tr>"
            f"<td>{esc(ticker)}</td>"
            f"<td>{esc(row.get('strategy'))}</td>"
            f"<td>{esc(row.get('expiration'))}</td>"
            f"<td>{esc(row.get('strikes'))}</td>"
            f"<td>{esc(row.get('score'))}</td>"
            f"<td>{'-' if ivr is None else f'{ivr:.0f}'}</td>"
            f"<td>{esc(_classify_ivr(ivr))}</td>"
            "</tr>"
        )
    return "\n".join(rows)


def render_reduced(categories: dict[str, list[dict[str, Any]]]) -> str:
    rows = []
    for row in categories["reduced"]:
        rationale = row.get("rationale") or {}
        reasons = " | ".join(
            str(value)
            for value in (
                rationale.get("adaptive_sizing"),
                rationale.get("profile"),
                rationale.get("correlation"),
            )
            if value
        )
        rows.append(
            "<tr>"
            f"<td>{esc(row.get('ticker'))}</td>"
            f"<td>{esc(row.get('strategy'))}</td>"
            f"<td>{esc(row.get('expiration'))}</td>"
            f"<td>{esc(row.get('strikes'))}</td>"
            f"<td>{float(row.get('size_multiplier') or 0):.2f}</td>"
            f"<td>{esc(row.get('score'))}</td>"
            f"<td>{esc(reasons or 'see plan for details')}</td>"
            "</tr>"
        )
    return "\n".join(rows)


BROKER_STATUS_LABELS = {
    "MISSING_POSITION": ("PHANTOM", "bad"),
    "PARTIAL_POSITION": ("PARTIAL", "warn"),
    "POSITION_UNKNOWN": ("UNKNOWN", "warn"),
    "POSITION_FOUND": ("OK", "ok"),
}


def broker_status_badge(row: dict[str, Any]) -> str:
    status = row.get("broker_position_status")
    if status is None:
        return "<td>-</td>"
    label, tone = BROKER_STATUS_LABELS.get(str(status), (str(status), "warn"))
    return f"<td><span class='badge {tone}'>{esc(label)}</span></td>"


def management_row(row: dict[str, Any]) -> str:
    threat = row.get("strike_threat", {})
    events = ", ".join(
        f"{event.get('event_type')} {event.get('date')}"
        for event in row.get("event_span", [])
    )
    roll = row.get("roll_proposal") or {}
    roll_text = "-"
    if roll.get("status") == "CREDIT_AVAILABLE":
        roll_text = (
            f"{roll.get('to_expiration')} {roll.get('to_strike')} "
            f"credit {roll.get('net_credit')}"
        )
    return (
        "<tr>"
        f"<td><span class='pill {esc(str(row.get('urgency', '')).lower())}'>{esc(row.get('urgency'))}</span></td>"
        f"<td>{esc(row.get('ticker'))}</td>"
        f"<td>{esc(row.get('strategy'))}</td>"
        f"<td>{esc(row.get('dte'))}</td>"
        f"<td>{esc(row.get('unrealized_pnl_pct'))}</td>"
        f"<td>{esc(row.get('action'))}</td>"
        f"{broker_status_badge(row)}"
        f"<td>{esc(threat.get('status'))}</td>"
        f"<td>{esc(events or '-')}</td>"
        f"<td>{esc(roll_text)}</td>"
        f"<td>{esc((row.get('reasons') or [''])[0])}</td>"
        "</tr>"
    )


def is_urgent_action(row: dict[str, Any]) -> bool:
    if row.get("urgency") == "HIGH":
        return True
    return row.get("broker_position_status") not in (None, "POSITION_FOUND")


def render_urgent_management(management: dict[str, Any]) -> str:
    # Phantom/partial/unknown rows pinned above plain HIGH-urgency rows.
    def pin_key(row: dict[str, Any]) -> int:
        return 0 if row.get("broker_position_status") not in (None, "POSITION_FOUND") else 1

    urgent = sorted(
        (row for row in management.get("actions", []) if is_urgent_action(row)),
        key=pin_key,
    )
    rows = [management_row(row) for row in urgent[:25]]
    if not rows:
        rows.append("<tr><td colspan='11'>No urgent position actions.</td></tr>")
    return "\n".join(rows)


def render_management(management: dict[str, Any]) -> str:
    rows = [management_row(row) for row in management.get("actions", [])[:25]]
    if not rows:
        rows.append("<tr><td colspan='11'>No open position management actions.</td></tr>")
    return "\n".join(rows)


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


def render_pop_calibration(scorecard: dict[str, Any]) -> str:
    rows = []
    for bucket, row in scorecard.get("pop_calibration", {}).get("buckets", {}).items():
        rows.append(
            "<tr>"
            f"<td>{esc(bucket)}</td>"
            f"<td>{esc(row.get('count'))}</td>"
            f"<td>{float(row.get('expected_pop_pct') or 0):.1f}%</td>"
            f"<td>{float(row.get('realized_win_rate_pct') or 0):.1f}%</td>"
            f"<td>{float(row.get('calibration_error_pct') or 0):+.1f}%</td>"
            "</tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='5'>No closed trades with recorded POP.</td></tr>")
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


def render_execution_breakdown(execution_analytics: dict[str, Any]) -> str:
    rows = []
    for strategy, row in execution_analytics.get("by_strategy", {}).items():
        rows.append(
            "<tr>"
            f"<td>{esc(strategy)}</td>"
            f"<td>{esc(row.get('tickets'))}</td>"
            f"<td>{float(row.get('fill_rate') or 0):.1f}%</td>"
            f"<td>{float(row.get('quantity_fill_rate') or 0):.1f}%</td>"
            f"<td>{float(row.get('avg_credit_improvement') or 0):+.3f}</td>"
            f"<td>{esc(row.get('floor_violations'))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='6'>No execution history.</td></tr>")
    return "\n".join(rows)


def render_execution_attribution(execution_history: dict[str, Any]) -> str:
    rows = []
    for strategy, row in execution_history.get("strategy_adjustments", {}).items():
        metrics = row.get("metrics", {})
        rows.append(
            "<tr>"
            f"<td>{esc(strategy)}</td>"
            f"<td>{esc(row.get('signal'))}</td>"
            f"<td>{float(row.get('score_adjustment') or 0):+.1f}</td>"
            f"<td>{float(row.get('size_multiplier') or 0):.2f}</td>"
            f"<td>{esc(row.get('sample_size'))}</td>"
            f"<td>{float(metrics.get('fill_rate') or 0):.1f}%</td>"
            f"<td>{float(metrics.get('avg_credit_improvement') or 0):+.3f}</td>"
            f"<td>{money(metrics.get('fees_per_contract'))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='8'>No durable execution attribution.</td></tr>")
    return "\n".join(rows)


def render_scenario_stress(scenario_stress: dict[str, Any]) -> str:
    rows = []
    for row in scenario_stress.get("scenarios", []):
        rows.append(
            "<tr>"
            f"<td>{esc(row.get('name'))}</td>"
            f"<td>{float(row.get('market_shock_pct') or 0):+.1f}%</td>"
            f"<td>{float(row.get('vol_shock_pct') or 0):+.1f}%</td>"
            f"<td>{money(row.get('estimated_pnl'))}</td>"
            f"<td>{float(row.get('estimated_pnl_pct_nav') or 0):+.2f}%</td>"
            "</tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='5'>No scenario stress report.</td></tr>")
    return "\n".join(rows)


def render_allocation(allocation: dict[str, Any]) -> str:
    rows = []
    for row in allocation.get("selected", []):
        rows.append(
            "<tr>"
            f"<td>{esc(row.get('rank'))}</td>"
            f"<td>{esc(row.get('ticker'))}</td>"
            f"<td>{esc(row.get('strategy'))}</td>"
            f"<td>{float(row.get('objective_score') or 0):.1f}</td>"
            f"<td>{money(row.get('capital'))}</td>"
            f"<td>{money(row.get('tail_loss'))}</td>"
            f"<td>{float(row.get('delta_change') or 0):+.1f}</td>"
            "</tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='7'>No portfolio allocation report.</td></tr>")
    return "\n".join(rows)


def render_validation(validation: dict[str, Any]) -> str:
    rows = []
    scopes = {"OVERALL": validation.get("overall", {}), **validation.get("by_strategy", {})}
    for scope, row in scopes.items():
        rows.append(
            "<tr>"
            f"<td>{esc(scope)}</td>"
            f"<td>{esc(row.get('status'))}</td>"
            f"<td>{esc(row.get('valid_fold_count'))}</td>"
            f"<td>{float(row.get('profitable_fold_pct') or 0):.1f}%</td>"
            f"<td>{money(row.get('avg_oos_expectancy'))}</td>"
            f"<td>{esc(row.get('avg_selected_threshold'))}</td>"
            f"<td>{float(row.get('threshold_std') or 0):.2f}</td>"
            "</tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='7'>No walk-forward validation report.</td></tr>")
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


def card(label: str, value: str, tone: str = "") -> str:
    return f"<div class='card {tone}'>{esc(label)}<b>{value}</b></div>"


STYLE = """
    :root { color-scheme: light; --ink:#18202f; --muted:#667085; --line:#d9dee8; --bg:#f7f9fc; --panel:#ffffff; --ok:#0b7a53; --warn:#9a5b00; --bad:#b42318; }
    body { margin:0; font-family: Segoe UI, Arial, sans-serif; background:var(--bg); color:var(--ink); }
    header { padding:20px 32px 14px; border-bottom:1px solid var(--line); background:var(--panel); }
    h1 { margin:0 0 6px; font-size:24px; font-weight:650; }
    h2 { margin:30px 0 12px; font-size:19px; border-bottom:2px solid var(--line); padding-bottom:6px; }
    h3 { margin:20px 0 8px; font-size:15px; }
    main { padding:6px 32px 36px; }
    .muted { color:var(--muted); }
    .meta { margin:2px 0; }
    .chip { display:inline-block; padding:2px 9px; border-radius:999px; font-size:12px; font-weight:650; background:#eef2f7; margin-left:6px; }
    .banner { margin:10px 0 0; padding:10px 14px; border-radius:8px; font-weight:650; font-size:14px; }
    .banner.warn { background:#fff4dc; color:var(--warn); border:1px solid #f0d9a8; }
    .banner.bad { background:#fdecec; color:var(--bad); border:1px solid #f3c6c2; }
    .regime-line { margin-top:10px; font-size:15px; }
    .regime-line b { font-size:16px; }
    nav.toc { margin-top:12px; }
    nav.toc a { display:inline-block; margin-right:14px; color:#175cd3; text-decoration:none; font-weight:650; font-size:13px; }
    .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin:10px 0 4px; }
    .card { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px; font-size:13px; color:var(--muted); }
    .card b { display:block; font-size:22px; margin-top:4px; color:var(--ink); }
    .card.bad b { color:var(--bad); }
    .card.ok b { color:var(--ok); }
    table { width:100%; border-collapse:collapse; background:var(--panel); border:1px solid var(--line); border-radius:8px; overflow:hidden; }
    th,td { padding:9px 10px; border-bottom:1px solid var(--line); text-align:left; font-size:13px; vertical-align:top; }
    th { background:#eef2f7; font-weight:650; }
    th[data-sort] { cursor:pointer; user-select:none; }
    tr:last-child td { border-bottom:0; }
    .pill,.priority,.badge { display:inline-block; padding:2px 7px; border-radius:999px; font-size:12px; font-weight:650; }
    .approve,.low,.badge.ok { background:#e8f5ef; color:var(--ok); }
    .reduce,.medium,.badge.warn { background:#fff4dc; color:var(--warn); }
    .reject,.high,.badge.bad { background:#fdecec; color:var(--bad); }
    .note { margin:6px 0 10px; font-size:13px; color:var(--muted); }
    .note.warn { color:var(--warn); font-weight:650; }
    .links a { display:inline-block; margin-right:10px; color:#175cd3; text-decoration:none; }
    details.section { background:var(--panel); border:1px solid var(--line); border-radius:8px; margin:10px 0; padding:0 14px; }
    details.section > summary { cursor:pointer; padding:12px 0; font-weight:650; font-size:15px; color:var(--ink); }
    details.section > .body { padding:2px 0 14px; }
    details summary { color:#175cd3; cursor:pointer; }
    details div { margin-top:5px; max-width:420px; color:var(--muted); }
    details.section > .body div { max-width:none; color:inherit; margin-top:0; }
    pre.brief { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px; overflow-x:auto; font-size:13px; line-height:1.45; }
    .split { display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr); gap:16px; }
    footer { padding:14px 32px 28px; color:var(--muted); font-size:12px; }
    @media (max-width:900px) { header,main,footer { padding-left:16px; padding-right:16px; } .split { grid-template-columns:1fr; } .table-wrap { overflow-x:auto; } }
"""

SCRIPT = """
    document.querySelectorAll('th[data-sort]').forEach((header) => {
      header.addEventListener('click', () => {
        const table = header.closest('table');
        const body = table.tBodies[0];
        const index = Array.from(header.parentNode.children).indexOf(header);
        const ascending = header.dataset.direction !== 'asc';
        const rows = Array.from(body.rows);
        rows.sort((a, b) => {
          const left = a.cells[index].innerText.trim().replace(/[$,%]/g, '');
          const right = b.cells[index].innerText.trim().replace(/[$,%]/g, '');
          const leftNumber = Number(left);
          const rightNumber = Number(right);
          const result = Number.isNaN(leftNumber) || Number.isNaN(rightNumber)
            ? left.localeCompare(right)
            : leftNumber - rightNumber;
          return ascending ? result : -result;
        });
        rows.forEach((row) => body.appendChild(row));
        header.dataset.direction = ascending ? 'asc' : 'desc';
      });
    });
"""


def collapsed(title: str, body: str, *, open_: bool = False) -> str:
    open_attr = " open" if open_ else ""
    return (
        f"<details class='section'{open_attr}><summary>{title}</summary>"
        f"<div class='body'>{body}</div></details>"
    )


def build_dashboard(
    *,
    plan: dict[str, Any],
    alerts: dict[str, Any],
    tickets: dict[str, Any],
    manifest: dict[str, Any],
    base: Path,
    analytics: dict[str, Any] | None = None,
    feedback: dict[str, Any] | None = None,
    reconciliation: dict[str, Any] | None = None,
    execution_analytics: dict[str, Any] | None = None,
    execution_history: dict[str, Any] | None = None,
    database_maintenance: dict[str, Any] | None = None,
    health: dict[str, Any] | None = None,
    scenario_stress: dict[str, Any] | None = None,
    allocation: dict[str, Any] | None = None,
    validation: dict[str, Any] | None = None,
    drift: dict[str, Any] | None = None,
    scorecard: dict[str, Any] | None = None,
    management: dict[str, Any] | None = None,
    brief_text: str | None = None,
    iv_ranks: dict[str, Any] | None = None,
) -> str:
    analytics = analytics or plan.get("historical_analytics", {})
    feedback = feedback or {}
    reconciliation_wrapper = reconciliation or {}
    lifecycle_counts = reconciliation_wrapper.get("ticket_lifecycle", {})
    reconciliation = reconciliation_wrapper.get("reconciliation", reconciliation_wrapper)
    recon_summary = reconciliation.get("summary", {})
    execution_analytics = execution_analytics or {}
    execution_summary = execution_analytics.get("summary", {})
    execution_history = execution_history or {}
    history_summary = execution_history.get("summary", {})
    funnel = manifest.get("opportunity_funnel", {})
    database_maintenance = database_maintenance or {}
    health = health or {}
    scenario_stress = scenario_stress or {}
    stress_summary = scenario_stress.get("summary", {})
    allocation = allocation or {}
    allocation_summary = allocation.get("summary", {})
    validation = validation or {}
    validation_summary = validation.get("summary", {})
    drift = drift or {}
    drift_summary = drift.get("summary", {})
    drift_comparison = drift.get("comparison", {})
    scorecard = scorecard or {}
    pop_summary = scorecard.get("pop_calibration", {})
    monthly = scorecard.get("monthly", {})
    latest_month = monthly[max(monthly)] if monthly else {}
    management = management or {}
    management_summary = management.get("summary", {})
    database_status = "N/A" if not database_maintenance else ("OK" if database_maintenance.get("ok") else "FAIL")
    health_status = "N/A" if not health else ("OK" if health.get("ok") else "FAIL")
    plan_summary = plan.get("summary", {})
    alert_summary = alerts.get("summary", {})
    overall = analytics.get("overall", {})
    drawdown = analytics.get("drawdown", {})
    as_of = manifest.get("created_at") or datetime.now().isoformat()
    profile = manifest.get("profile")
    links = render_links(manifest, base)

    # --- header: regime / sizing / exceptions -------------------------------
    iv_payload = iv_ranks or {}
    context = regime_context(brief_text, iv_payload, allocation)
    regime_bits = [f"Regime: <b>{esc(context['regime'] or 'UNKNOWN')}</b>"]
    if context["regime_source"]:
        regime_bits.append(f"<span class='muted'>({esc(context['regime_source'])})</span>")
    regime_bits.append(f"· Sizing: <b>{esc(context['sizing'] or 'cautious (assumed)')}</b>")
    if context["sizing_source"]:
        regime_bits.append(f"<span class='muted'>({esc(context['sizing_source'])})</span>")
    regime_line = " ".join(regime_bits)
    conservative_banner = (
        f"<div class='banner warn'>⚠ {esc(CONSERVATIVE_BANNER)}</div>"
        if context["conservative"]
        else ""
    )
    exception_strip = render_exception_strip(management_summary, recon_summary)

    # --- §1 do now -----------------------------------------------------------
    gate_ranks: dict[str, float] = iv_payload.get("iv_ranks") or {}
    categories = categorize_executable_tickets(tickets, gate_ranks or None)
    if gate_ranks:
        gate_note = (
            f"<div class='note'>IVR gate evaluated {esc(iv_payload.get('as_of') or '')} — "
            f"{len(gate_ranks)} tickers checked, IVR&lt;50 demoted below. "
            "Matches the 10:30 Telegram message.</div>"
        )
    else:
        gate_note = (
            "<div class='note warn'>IVR gate NOT evaluated for this run (no iv_ranks.json) — "
            "list is ungated and may include IVR&lt;50 names the 10:30 Telegram message holds.</div>"
        )
    held_section = ""
    if categories["held_by_ivr"]:
        held_section = f"""
    <h3>Held by IVR ({len(categories["held_by_ivr"])}) — not executable</h3>
    <div class='note'>IVR &lt; 50: short-premium edge below median. Re-enters when IVR recovers.</div>
    <div class="table-wrap"><table class="sortable">
      <thead><tr><th data-sort>Ticker</th><th data-sort>Strategy</th><th data-sort>Expiration</th><th data-sort>Strikes</th><th data-sort>Score</th><th data-sort>IVR</th><th>IV Regime</th></tr></thead>
      <tbody>{render_held_by_ivr(categories, gate_ranks)}</tbody>
    </table></div>"""
    reduced_section = ""
    if categories["reduced"]:
        reduced_section = f"""
    <h3>Reduced Size ({len(categories["reduced"])})</h3>
    <div class="table-wrap"><table class="sortable">
      <thead><tr><th data-sort>Ticker</th><th data-sort>Strategy</th><th data-sort>Expiration</th><th data-sort>Strikes</th><th data-sort>Size ×</th><th data-sort>Score</th><th>Why reduced</th></tr></thead>
      <tbody>{render_reduced(categories)}</tbody>
    </table></div>"""
    snapshot_meta = management.get("broker_snapshot", {})
    snapshot_note = ""
    if snapshot_meta.get("snapshot_at"):
        availability = "available" if snapshot_meta.get("positions_available") else "UNAVAILABLE"
        snapshot_note = (
            f"<div class='note'>Broker snapshot {esc(snapshot_meta.get('snapshot_at'))} "
            f"({esc(snapshot_meta.get('source') or 'unknown source')}, positions {availability}).</div>"
        )
    phantom_total = sum(
        int(management_summary.get(key) or 0)
        for key in ("phantom_positions", "partial_positions", "unknown_positions")
    )
    do_now_cards = "".join(
        [
            card("Executable", str(len(categories["executable"]))),
            card("Held by IVR", str(len(categories["held_by_ivr"]))),
            card("Open Positions", esc(management_summary.get("open_trades", 0))),
            card("High Urgency", esc(management_summary.get("high_urgency", 0)),
                 "bad" if int(management_summary.get("high_urgency") or 0) else ""),
            card("Phantom / Mismatch", str(phantom_total), "bad" if phantom_total else "ok"),
            card("High Alerts", esc(alert_summary.get("high", 0)),
                 "bad" if int(alert_summary.get("high") or 0) else ""),
        ]
    )
    open_summary_line = (
        f"{management_summary.get('open_trades', 0)} open · "
        f"{management_summary.get('close', 0)} close · "
        f"{management_summary.get('roll_or_close', 0)} roll · "
        f"{management_summary.get('review', 0)} review · "
        f"{management_summary.get('hold', 0)} hold"
    )

    # --- §2 what changed ------------------------------------------------------
    changed_cards = "".join(
        [
            card("Drift Status", esc(drift_summary.get("status") or "N/A")),
            card("Expectancy Change", money(drift_comparison.get("expectancy_change"))),
            card("Validation", esc(validation_summary.get("status") or "N/A")),
            card("Alerts", esc(alert_summary.get("total", len(alerts.get("alerts", []))))),
        ]
    )

    # --- §3 full picture ------------------------------------------------------
    full_sections = []
    if brief_text:
        full_sections.append(collapsed(
            "Market Context (morning brief — incl. 📊 Watchlist Stocks)",
            f"<pre class='brief'>{esc(brief_text)}</pre>",
        ))
    full_sections.append(collapsed("Action Plan", f"""
    <div class="table-wrap"><table class="sortable">
      <thead><tr><th data-sort>Action</th><th data-sort>Ticker</th><th data-sort>Strategy</th><th data-sort>Score</th><th data-sort>Size</th><th data-sort>Limit</th><th data-sort>Floor</th><th data-sort>Exec</th><th data-sort>Profile</th><th>Rationale</th></tr></thead>
      <tbody>{render_actions(plan)}</tbody>
    </table></div>"""))
    full_sections.append(collapsed("Portfolio Allocation", f"""
    <div class="table-wrap"><table class="sortable">
      <thead><tr><th data-sort>Rank</th><th data-sort>Ticker</th><th data-sort>Strategy</th><th data-sort>Objective</th><th data-sort>Capital</th><th data-sort>Tail Loss</th><th data-sort>Delta</th></tr></thead>
      <tbody>{render_allocation(allocation)}</tbody>
    </table></div>"""))
    full_sections.append(collapsed("Scenario Stress", f"""
    <div class="cards">
      {card("Worst Stress", esc(stress_summary.get("worst_scenario") or "N/A"))}
      {card("Stress P&L", money(stress_summary.get("worst_pnl")))}
      {card("Stress % NAV", f"{float(stress_summary.get('worst_pnl_pct_nav') or 0):+.2f}%")}
    </div>
    <div class="table-wrap"><table class="sortable">
      <thead><tr><th data-sort>Scenario</th><th data-sort>Market</th><th data-sort>Vol</th><th data-sort>Est. P&amp;L</th><th data-sort>% NAV</th></tr></thead>
      <tbody>{render_scenario_stress(scenario_stress)}</tbody>
    </table></div>"""))
    full_sections.append(collapsed("Score-Band Performance &amp; Strategy Calibration", f"""
    <div class="split">
      <section>
        <h3>Score-Band Performance</h3>
        <div class="table-wrap"><table class="sortable">
          <thead><tr><th data-sort>Score</th><th data-sort>N</th><th data-sort>Win</th><th data-sort>Expectancy</th><th data-sort>Return/Risk</th><th data-sort>PF</th></tr></thead>
          <tbody>{render_score_bands(analytics)}</tbody>
        </table></div>
      </section>
      <section>
        <h3>Strategy Calibration</h3>
        <div class="table-wrap"><table class="sortable">
          <thead><tr><th data-sort>Strategy</th><th data-sort>Signal</th><th data-sort>Multiplier</th><th data-sort>N</th><th data-sort>Expectancy</th></tr></thead>
          <tbody>{render_strategy_feedback(feedback)}</tbody>
        </table></div>
      </section>
    </div>"""))
    full_sections.append(collapsed("POP Calibration &amp; Monthly Scorecard", f"""
    <div class="cards">
      {card("POP Samples", esc(pop_summary.get("sample_size", 0)))}
      {card("Monthly Account Return", f"{float(latest_month.get('account_return_pct') or 0):+.2f}%")}
      {card("Monthly Excess vs SPY", esc('N/A' if latest_month.get('excess_return_vs_spy_pct') is None else f"{float(latest_month.get('excess_return_vs_spy_pct')):+.2f}%"))}
      {card("Expectancy", money(overall.get("expectancy")))}
      {card("Max Drawdown", money(drawdown.get("max_drawdown")))}
      {card("Min Score", f"{float(feedback.get('recommended_min_score') or 0):.0f}")}
    </div>
    <div class="table-wrap"><table class="sortable">
      <thead><tr><th data-sort>POP Bucket</th><th data-sort>N</th><th data-sort>Expected</th><th data-sort>Realized Win</th><th data-sort>Error</th></tr></thead>
      <tbody>{render_pop_calibration(scorecard)}</tbody>
    </table></div>"""))
    full_sections.append(collapsed("Execution Quality &amp; Attribution", f"""
    <div class="cards">
      {card("Fill Rate", esc('NO_SUBMITTED_HISTORY' if execution_summary.get('status') == 'NO_SUBMITTED_HISTORY' else f"{float(execution_summary.get('fill_rate') or 0):.0f}%"))}
      {card("Quantity Fill", f"{float(execution_summary.get('quantity_fill_rate') or 0):.0f}%")}
      {card("Credit vs Plan", f"{float(execution_summary.get('avg_credit_improvement') or 0):+.3f}")}
      {card("Execution Fees", money(execution_summary.get("total_fees")))}
      {card("Avg Fill Delay", f"{float(execution_summary.get('avg_fill_delay_seconds') or 0):.0f}s")}
      {card("Execution History", esc(history_summary.get("count", 0)))}
      {card("Fees / Contract", money(history_summary.get("fees_per_contract")))}
    </div>
    <h3>Execution Quality</h3>
    <div class="table-wrap"><table class="sortable">
      <thead><tr><th data-sort>Strategy</th><th data-sort>Tickets</th><th data-sort>Fill Rate</th><th data-sort>Quantity Fill</th><th data-sort>Credit vs Plan</th><th data-sort>Floor Violations</th></tr></thead>
      <tbody>{render_execution_breakdown(execution_analytics)}</tbody>
    </table></div>
    <h3>Durable Execution Attribution</h3>
    <div class="table-wrap"><table class="sortable">
      <thead><tr><th data-sort>Strategy</th><th data-sort>Signal</th><th data-sort>Score</th><th data-sort>Size</th><th data-sort>N</th><th data-sort>Fill Rate</th><th data-sort>Credit vs Plan</th><th data-sort>Fees / Contract</th></tr></thead>
      <tbody>{render_execution_attribution(execution_history)}</tbody>
    </table></div>"""))
    full_sections.append(collapsed("Execution Tickets — All Candidates (incl. non-executable)", f"""
    <div class="note">Full tickets.json contents. Executable subset (IVR-gated) is in section 1.</div>
    <div class="table-wrap"><table class="sortable">
      <thead><tr><th>Decision</th><th>Ticker</th><th>Strategy</th><th>Expiration</th><th>Strikes</th><th>Order</th><th>Limit</th><th>Floor</th><th>Max Loss</th></tr></thead>
      <tbody>{render_tickets(tickets)}</tbody>
    </table></div>"""))
    full_sections.append(collapsed("System &amp; Pipeline", f"""
    <div class="cards">
      {card("Approved", esc(plan_summary.get("approve", 0)))}
      {card("Reduced", esc(plan_summary.get("reduce", 0)))}
      {card("Rejected", esc(plan_summary.get("reject", 0)))}
      {card("Planning Candidates", esc(funnel.get("morning_candidates", plan_summary.get("approve", 0) + plan_summary.get("reduce", 0))))}
      {card("Ready for Review", esc(lifecycle_counts.get("READY", len(tickets.get("tickets", [])))))}
      {card("Submitted Orders", esc(lifecycle_counts.get("SUBMITTED", 0)))}
      {card("Working Orders", esc(lifecycle_counts.get("WORKING", 0)))}
      {card("Partial Fills", esc(lifecycle_counts.get("PARTIAL", 0)))}
      {card("Filled Orders", esc(int(lifecycle_counts.get("FILLED", 0) or 0) + int(lifecycle_counts.get("OVERFILLED", 0) or 0)))}
      {card("Active Tickets", esc(recon_summary.get("active_tickets", 0)))}
      {card("Expired Tickets", esc(recon_summary.get("expired_tickets", 0)))}
      {card("Basket Selected", esc(allocation_summary.get("selected", 0)))}
      {card("Capital Allocated", money(allocation_summary.get("capital_allocated")))}
      {card("Tail Budget Used", f"{float(allocation_summary.get('tail_budget_utilization_pct') or 0):.1f}%")}
      {card("Allocation Risk Model", esc(allocation.get("limits", {}).get("risk_model") or "N/A"))}
      {card("Projected 95% ES", money(allocation_summary.get("projected_expected_shortfall_95")))}
      {card("OOS Expectancy", money(validation_summary.get("avg_oos_expectancy")))}
      {card("DB Integrity", database_status, "bad" if database_status == "FAIL" else "")}
      {card("Health", health_status, "bad" if health_status == "FAIL" else "")}
    </div>
    <div class="links">{links}</div>"""))
    full_picture = "\n".join(full_sections)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Quant Tools Dashboard</title>
  <style>{STYLE}</style>
</head>
<body>
  <header>
    <h1>Quant Tools Dashboard{f"<span class='chip'>{esc(profile)} run</span>" if profile else ""}</h1>
    <div class="muted meta">Generated {esc(as_of)} · read-only — staging stays in the CLI</div>
    <div class="regime-line">{regime_line}</div>
    {conservative_banner}
    {exception_strip}
    <nav class="toc">
      <a href="#do-now">1 · Do Now</a>
      <a href="#changed">2 · What Changed</a>
      <a href="#full-picture">3 · Full Picture</a>
    </nav>
  </header>
  <main>
    <section id="do-now">
      <h2>1 · Do Now</h2>
      <div class="cards">{do_now_cards}</div>
      <h3>Executable Candidates ({len(categories["executable"])})</h3>
      {gate_note}
      <div class="table-wrap"><table class="sortable">
        <thead><tr><th data-sort>Ticker</th><th data-sort>Strategy</th><th data-sort>Expiration</th><th data-sort>Strikes</th><th data-sort>Order</th><th data-sort>Limit</th><th data-sort>Floor</th><th data-sort>Qty</th><th data-sort>Size ×</th><th data-sort>Score</th><th data-sort>Max Loss</th></tr></thead>
        <tbody>{render_executable(categories)}</tbody>
      </table></div>
      {held_section}
      {reduced_section}
      <h3>Open Positions — Urgent &amp; Exceptions</h3>
      <div class="note">{esc(open_summary_line)}</div>
      {snapshot_note}
      <div class="table-wrap"><table class="sortable">
        <thead><tr><th data-sort>Urgency</th><th data-sort>Ticker</th><th data-sort>Strategy</th><th data-sort>DTE</th><th data-sort>P&amp;L %</th><th data-sort>Action</th><th data-sort>Broker</th><th data-sort>Strike</th><th>Events</th><th>Credit Roll</th><th>Reason</th></tr></thead>
        <tbody>{render_urgent_management(management)}</tbody>
      </table></div>
    </section>
    <section id="changed">
      <h2>2 · What Changed</h2>
      <div class="cards">{changed_cards}</div>
      <h3>Alerts</h3>
      <div class="table-wrap"><table class="sortable">
        <thead><tr><th>Priority</th><th>Kind</th><th>Title</th><th>Detail</th></tr></thead>
        <tbody>{render_alerts(alerts)}</tbody>
      </table></div>
      <h3>Walk-Forward Validation</h3>
      <div class="table-wrap"><table class="sortable">
        <thead><tr><th data-sort>Scope</th><th data-sort>Status</th><th data-sort>Folds</th><th data-sort>Profitable</th><th data-sort>OOS Expectancy</th><th data-sort>Threshold</th><th data-sort>Threshold Std</th></tr></thead>
        <tbody>{render_validation(validation)}</tbody>
      </table></div>
      <h3>Open Position Management (all)</h3>
      <div class="table-wrap"><table class="sortable">
        <thead><tr><th data-sort>Urgency</th><th data-sort>Ticker</th><th data-sort>Strategy</th><th data-sort>DTE</th><th data-sort>P&amp;L %</th><th data-sort>Action</th><th data-sort>Broker</th><th data-sort>Strike</th><th>Events</th><th>Credit Roll</th><th>Reason</th></tr></thead>
        <tbody>{render_management(management)}</tbody>
      </table></div>
    </section>
    <section id="full-picture">
      <h2>3 · Full Picture</h2>
      {full_picture}
    </section>
  </main>
  <footer>
    Read-only dashboard. Order staging requires the explicit CLI transition (quant.py stage) — no order can be placed from this page.
  </footer>
  <script>{SCRIPT}</script>
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
    ap.add_argument("--reconciliation")
    ap.add_argument("--execution-analytics")
    ap.add_argument("--execution-history")
    ap.add_argument("--database-maintenance")
    ap.add_argument("--health")
    ap.add_argument("--scenario-stress")
    ap.add_argument("--allocation")
    ap.add_argument("--validation")
    ap.add_argument("--drift")
    ap.add_argument("--scorecard")
    ap.add_argument("--management")
    ap.add_argument("--brief", help="Morning brief text (brief.out) for market context / regime parse")
    ap.add_argument("--iv-ranks", help="iv_ranks.json persisted by the 10:30 executable scan")
    ap.add_argument("--output")
    args = ap.parse_args()

    base = Path(args.report_dir) if args.report_dir else Path.cwd()
    plan_path = Path(args.plan) if args.plan else base / "plan.json"
    alerts_path = Path(args.alerts) if args.alerts else base / "alerts.json"
    tickets_path = Path(args.tickets) if args.tickets else base / "tickets.json"
    manifest_path = Path(args.manifest) if args.manifest else base / "manifest.json"
    analytics_path = Path(args.analytics) if args.analytics else base / "analytics.json"
    feedback_path = Path(args.feedback) if args.feedback else base / "feedback.json"
    reconciliation_path = Path(args.reconciliation) if args.reconciliation else base / "reconciliation.json"
    execution_path = Path(args.execution_analytics) if args.execution_analytics else base / "execution_analytics.json"
    execution_history_path = Path(args.execution_history) if args.execution_history else base / "execution_history.json"
    database_path = Path(args.database_maintenance) if args.database_maintenance else base / "database_maintenance.json"
    health_path = Path(args.health) if args.health else base / "health.json"
    scenario_path = Path(args.scenario_stress) if args.scenario_stress else base / "scenario_stress.json"
    allocation_path = Path(args.allocation) if args.allocation else base / "allocation.json"
    validation_path = Path(args.validation) if args.validation else base / "validation.json"
    drift_path = Path(args.drift) if args.drift else base / "drift.json"
    scorecard_path = Path(args.scorecard) if args.scorecard else base / "scorecard.json"
    management_path = Path(args.management) if args.management else base / "management.json"
    brief_path = Path(args.brief) if args.brief else base / "brief.out"
    iv_ranks_path = Path(args.iv_ranks) if args.iv_ranks else base / "iv_ranks.json"
    output_path = Path(args.output) if args.output else base / "dashboard.html"

    brief_text = brief_path.read_text(encoding="utf-8") if brief_path.is_file() else None

    html_out = build_dashboard(
        plan=read_json(plan_path),
        alerts=read_json(alerts_path),
        tickets=read_json(tickets_path),
        manifest=read_json(manifest_path),
        base=base,
        analytics=read_json(analytics_path),
        feedback=read_json(feedback_path),
        reconciliation=read_json(reconciliation_path),
        execution_analytics=read_json(execution_path),
        execution_history=read_json(execution_history_path),
        database_maintenance=read_json(database_path),
        health=read_json(health_path),
        scenario_stress=read_json(scenario_path),
        allocation=read_json(allocation_path),
        validation=read_json(validation_path),
        drift=read_json(drift_path),
        scorecard=read_json(scorecard_path),
        management=read_json(management_path),
        brief_text=brief_text,
        iv_ranks=read_json(iv_ranks_path),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html_out, encoding="utf-8")
    print(output_path)


if __name__ == "__main__":
    main()
