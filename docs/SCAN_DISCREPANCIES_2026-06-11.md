# Scan Discrepancies — 2026-06-11 10:31 ET Executable Scan

> **Status:** Held for debugging. No action taken on the scan.
> **Triggered by:** User flagged that the scan reported an open position to close, but they have no open positions.

## Confirmed state (source of truth)

| Source | NVDA CSP open? | Notes |
|---|---|---|
| `state/positions.json` | **No** | `"positions": {}`, `last_updated: 2026-06-11T10:16:35` |
| SQLite `broker_positions` table | **No** | 0 rows |
| SQLite `trades` table | **Yes (phantom)** | 1 row: `T20260604-001`, NVDA CSP, `status=OPEN`, `capital_at_risk=0.0`, `updated_at=2026-06-11T10:31:27` (touched by the scan) |
| Public.com live | **No** | (implied — user confirmed) |

The scan computed `-580%` of max profit and a `-200%` stop breach on `T20260604-001`, but the trade has no matching broker position and zero capital at risk. This is a stale journal row being treated as a real open position by the position-management stage.

## Discrepancies

### 1. Phantom open position (CRITICAL)
- **What scan said:** NVDA CSP, DTE=36d, P&L=-580% → CLOSE, stop_loss breach at -200% of max profit.
- **Reality:** No broker position. Trade row `T20260604-001` is the **test trade from 2026-06-04** that was `journal repair`-ed twice (expiration/strikes added, then strikes narrowed from 195/190 to 195). It is `status=OPEN` in the journal with `capital_at_risk=0.0`.
- **Likely root cause:** `position_management.py` reads `trades WHERE status=OPEN` and computes P&L against an entry cost/credit that doesn't exist (or against a stale option-chain snapshot). It does not appear to be cross-referencing `broker_positions` to confirm the trade is actually held.
- **Knock-on effect:** The `pretrade_check` / action-plan stage never flagged this as a "phantom" — it just generated a CLOSE action. No defensive check exists for "trade is OPEN in journal but missing from broker snapshot."

### 2. `reports/cron-2026-06-11/` directory missing
- The 10:31 ET executable scan was delivered to Telegram, but the canonical `reports/cron-2026-06-11/<timestamp>/` artifact dir was **not created** on disk.
- Normal pattern (per the 2026-06-10 sessions): `quant.py operator --profile executable` writes a full artifact set (tickets.json, operator_summary.md, risk.json, etc.). This run did not.
- Possible cause: scan was run via a no-agent cron wrapper that doesn't persist artifacts, OR the executable profile skips report-dir creation in a path the recent PR #3 refactor changed.
- **Debug action:** Check `ops/hermes/cron_executable_scan.py` and `hermes_ops.py` for the `ensure_report_dir` / `mkdir` call in the executable profile path. Compare to `cron_morning_workflow.py`.

### 3. Scan candidate list possibly truncated
- The 10:31 Telegram message header said **"EXECUTABLE (4 approved)"** but only **3 candidates were shown** in the captured snippet (QQQ 685/682, MSFT 375/370, NVDA 195/190). The 4th is missing from the rendered output.
- Possible cause: Telegram message truncation (4096-char limit), OR the scan actually only produced 3 candidates and the header counter is wrong.
- **Debug action:** Re-run the executable scan locally and diff the candidate count against the Telegram-rendered count. Check whether the 10:30 cron reports a different candidate list when run from disk.

### 4. MSFT strike / price sanity check missing
- Scan candidate: **MSFT BULL_PUT 2026-07-17 375.0/370.0**, credit $1.36, floor $1.24.
- No MSFT spot price in the most recent morning briefs (2026-06-10 or 2026-06-11) to confirm whether 375 is OTM/ITM.
- The 8:31 morning brief on 2026-06-11 did not include a SPY/QQQ/IWM/MSFT/AAPL/NVDA price block (it showed `nan` for everything on 2026-06-10 due to the yfinance NaN issue, but 2026-06-11's brief did include SPY/QQQ prices and IV columns — MSFT was missing from the prices list specifically).
- **Debug action:** Pull live MSFT spot. Verify the 375/370 strike is reasonable relative to spot (CSP at 375 means ~5% OTM if MSFT is around 395; deep ITM if MSFT has dropped to ~360).

### 5. IVR / score coherence
- Morning brief said: NVDA IVR 43 (cautious sell), MSFT IVR 45 (cautious sell), AAPL IVR 43, TSLA IVR 22 (buy premium).
- The scan still promoted NVDA and MSFT bull puts despite their "below-median cautious sell" classification. The brief's own guidance said "cautious sell" for both.
- This is consistent with the brief (cautious-sell ≠ don't-sell) but worth confirming the position_management / action_plan modules are weighting IVR correctly into the EXECUTABLE promotion (not just into the brief text).

## Self-flagged analysis errors (my read of the scan, 2026-06-11 10:35)

For honest record — items in my first response that were wrong or unsupported:

- I said **"MSFT was near 395"** as if from the morning brief. It was not — neither the 2026-06-10 nor 2026-06-11 morning brief showed an MSFT spot price. That number was invented.
- I said the **4th candidate "is presumably weaker"** because the snippet was truncated. I had no evidence for that — pure speculation.
- I called the QQQ 685/682 a **"standout"** because of the 63.3 score; I didn't account for the fact that the macro regime's 80/100 score is the highest in weeks, so the 57-63 score compression is a relative-weakness signal, not a strength.
- I said **"the scan is asking, not telling"** re: the NVDA CSP close — actually the scan was fabricating a position that doesn't exist. I should have questioned the existence of the position before reasoning about the action.

## To investigate when we resume debugging

1. **Phantom-position filter.** Add a defensive check in `position_management.py` (or `pretrade_check.py`): before computing P&L or recommending CLOSE, verify the trade row has a matching row in `broker_positions` (or in `state/positions.json`). If not, mark the trade `status=STALE` (new status) and exclude from action plan. Also surface a HIGH alert in the operator summary.
2. **Test-trade lifecycle.** Decide: are test trades (e.g. `T20260604-001`) supposed to live in the `trades` table at all? If yes, they need a `is_test=1` flag (or `status=TEST`) that the action plan filters out. If no, we need a `quant.py journal purge-test-trades` command.
3. **`T20260604-001` cleanup.** Manually set `status=CLOSED` (or `TEST`) with a `closed_at` timestamp and a `notes` entry citing this discrepancy log, so it stops polluting the 8:30 and 10:30 scans.
4. **Executable-profile artifact persistence.** Confirm whether the executable profile is supposed to write a report dir. If yes, the cron wrapper regression is in the PR #3 refactor (`cron_executable_scan.py` slimmer, delegates to `hermes_ops.py`). If no, document the design.
5. **Telegram 4-vs-3 candidate count.** Re-run scan locally; count candidates; diff against the cron-delivered message.
6. **MSFT price in morning brief.** Confirm why MSFT spot was missing from the 2026-06-11 brief's price block. May be a yfinance NaN issue (cf. `2c34104` fix), or a watchlist-rendering issue.

## Files to read first when debugging

- `scripts/position_management.py` — P&L computation, open-position filter
- `scripts/pretrade_check.py` — defensive checks against broker snapshot
- `scripts/operator_summary.py` — `is_test`/phantom filter (if any)
- `scripts/journal.py` — trade-row creation, test-trade handling
- `ops/hermes/cron_executable_scan.py` — executable-profile report-dir handling
- `scripts/hermes_ops.py` — shared helper for the slim cron wrappers
- `state/quant_tools.db` — `trades`, `broker_positions`, `tickets` (already inspected: phantom confirmed)
- `state/positions.json` — point-in-time broker mirror (already inspected: empty)

## Resolution log (added 2026-06-11)

| # | Discrepancy | Status | Fix |
|---|---|---|---|
| 1 | Phantom open position (CRITICAL) | **Fixed** in `fa1e567` (already shipped) | `build_management_report` + `mark_open_trades` take a `broker_snapshot`, call `reconcile_open_trades`; MISSING_POSITION → REVIEW/HIGH/PHANTOM (not CLOSE). Surfaces a `position_management_exception` alert and excludes the phantom from journal P&L alerts. |
| 2 | `reports/cron-2026-06-11/<ts>/` missing | **False alarm** + small robustness fix in `c1b1068` | Artifacts were correctly persisted at `reports/exec-2026-06-11/20260611-103058/` (the executable profile's parent dir is `exec-`, deliberately separate from the morning profile's `cron-`). The doc author looked in the wrong parent dir. Added a `latest_report_dir is None` guard so a future silent empty Telegram is impossible. |
| 3 | Telegram header "4 approved" but 3 shown | **Fixed** in `fa1e567` (already shipped) | `format_executable_tickets` now shows "showing N of M approved" when truncated. |
| 4 | MSFT spot missing from morning brief | **Fixed** in `9560cf5` | New `📊 WATCHLIST STOCKS` block in `daily_brief.build_brief` iterates over the watchlist minus indices already in MARKETS, renders the same emoji / chg / pct format, flags missing data as `(price unavailable)` instead of silently dropping. Verified live: MSFT $387.90, NVDA $202.39, AAPL $295.57, TSLA $392.42. |
| 5 | IVR / score coherence | **Fixed** in `914e4a9` | `format_executable_tickets` and `compose_executable_message` now accept an `iv_ranks: dict[ticker, IVR]`. APPROVE/STRONG tickets with IVR < 50 are demoted from EXECUTABLE to a `HELD BY IVR` section with the regime annotation (e.g. `IVR=43 (below-median (cautious sell))`). Threshold of 50 mirrors the existing portfolio-level IVRank guard in `portfolio_allocator.py:264`. `cron_executable_scan.fetch_iv_ranks_for_tickets` shells out to `iv_rank.py` for the actionable tickers and feeds the result in. End-to-end against the real 10:30 run: 4 candidates → 1 EXECUTABLE (QQQ IVR 88), 3 HELD BY IVR. |

**Test count** at resolution: 153 unit tests pass (was 102 at schema v4, 142 at fa1e567, 147 after c59e8e4, 149 after the three follow-up fixes, 153 after #5).
