# quant-tools — Public.com options & risk toolkit

Production trading toolkit for the Public.com brokerage account (`5OG66124`).
Python tools for live Public.com and yfinance data, decision analytics, risk
gating, execution preparation, and operator reporting. **No mocks, ever.**

## Layout

```
quant-tools/
├── README.md                  ← you are here
├── docs/
│   └── STRATEGY_RESEARCH.md   ← $30k allocation plan, 35+ strategies, June 2026 research
├── scripts/                   ← production Python (10 scripts + quant.py wrapper)
├── state/
│   └── positions.json         ← position_tracker.py state (cost basis per position)
└── reports/                   ← (empty — historical daily briefs land here if you save them)
```

## Quick run

All scripts **must** be run with `/usr/bin/python3.12` — the system `python3`
is 3.14 and won't import `public_api_sdk`.
`scripts/quant.py` uses the current interpreter by default; set `QUANT_PYTHON`
when you need to force the production runtime:

```bash
export QUANT_PYTHON=/usr/bin/python3.12
cd /home/chavez_f/code/quant-tools/scripts
/usr/bin/python3.12 quant.py macro --watchlist SPY QQQ NVDA AAPL MSFT TSLA
/usr/bin/python3.12 quant.py scan --watchlist SPY QQQ NVDA --strategies csp bull_put --min-dte 21 --max-dte 45
/usr/bin/python3.12 quant.py scan --watchlist SPY QQQ NVDA --strategies csp bull_put --ranked --max-expirations 2 --wing-widths 2.5 5 10
/usr/bin/python3.12 quant.py pretrade --candidates reports/scan.json --account-nav 30000
/usr/bin/python3.12 quant.py journal add --ticker SPY --strategy BULL_PUT --entry-credit 1.20 --capital-at-risk 380 --score 66 --thesis "defined risk, acceptable liquidity"
/usr/bin/python3.12 quant.py journal profiles --section ticker_strategy
/usr/bin/python3.12 quant.py plan --candidates reports/scan.json --portfolio reports/risk.json --journal state/trades.json --account-nav 30000
/usr/bin/python3.12 quant.py allocate --plan reports/plan.json --json
/usr/bin/python3.12 quant.py alerts --plan reports/plan.json --journal state/trades.json
/usr/bin/python3.12 quant.py tickets --plan reports/plan.json
/usr/bin/python3.12 quant.py dashboard --report-dir reports/latest
/usr/bin/python3.12 quant.py analytics --journal state/trades.json
/usr/bin/python3.12 quant.py feedback --journal state/trades.json
/usr/bin/python3.12 quant.py validate --journal state/trades.json --json
/usr/bin/python3.12 quant.py drift --journal state/trades.json --json
/usr/bin/python3.12 quant.py operator --report-dir reports
/usr/bin/python3.12 quant.py storage --journal state/trades.json --tickets reports/latest/tickets.json --portfolio reports/latest/risk.json
/usr/bin/python3.12 quant.py reconcile --journal state/trades.json --tickets reports/latest/tickets.json --broker-snapshot state/broker_snapshot.json
/usr/bin/python3.12 quant.py reconcile --journal state/trades.json --tickets reports/latest/tickets.json --broker-snapshot state/broker_snapshot.json --apply-updates --db state/quant_tools.db
/usr/bin/python3.12 quant.py execution-analytics --tickets reports/latest/tickets.json --reconciliation reports/latest/reconciliation.json
/usr/bin/python3.12 quant.py journal --db state/quant_tools.db add --ticker SPY --strategy BULL_PUT --entry-credit 1.10
/usr/bin/python3.12 quant.py db-maintenance --db state/quant_tools.db --json
/usr/bin/python3.12 quant.py verify --json
/usr/bin/python3.12 quant.py scenario-stress --portfolio reports/latest/risk.json --json
/usr/bin/python3.12 quant.py iv-rank --tickers SPY QQQ NVDA AAPL MSFT TSLA AMD
/usr/bin/python3.12 quant.py brief --watchlist SPY QQQ NVDA AAPL MSFT TSLA
```

## Verification

```bash
/usr/bin/python3.12 ~/.hermes/skills/mlops/quant-trading-toolkit/scripts/verify_toolkit.py
python -m unittest discover -s tests
python scripts/quant.py verify --json
```

The skill verifier runs 11 smoke tests across all tools. The local unit tests
cover OSI parsing, candidate scoring, pre-trade checks, journal P&L math, config
merging, and cache round-trips.

## Config

Defaults live in `config.example.json`. Copy it to `config.json` for local
watchlists, scan defaults, journal path, and risk limits. The unified runner
loads `config.json` automatically when present, or you can pass a specific file:

```bash
/usr/bin/python3.12 quant.py --config config.example.json scan --ranked
/usr/bin/python3.12 quant.py --config config.example.json discover --top 20
/usr/bin/python3.12 quant.py --config config.example.json plan --candidates reports/scan.json --portfolio reports/risk.json
/usr/bin/python3.12 quant.py --config config.example.json daily --dry-run
/usr/bin/python3.12 quant.py --config config.example.json dashboard --report-dir reports/latest
/usr/bin/python3.12 quant.py --config config.example.json operator --dry-run
```

The `operator` command produces a timestamped decision package containing
analytics, calibration feedback, discovery, scan, risk, plan, alerts, execution
tickets, broker reconciliation, a send-ready Markdown summary, and the sortable
HTML dashboard. It also synchronizes journal, ticket, position, fill, and
reconciliation records into `state/quant_tools.db` by default, then reports
fill rate, credit improvement or slippage versus plan, and execution-floor
violations by strategy. Deterministic market and volatility scenarios estimate
portfolio and position-level losses before tickets are reviewed. The portfolio
allocator ranks actionable trades by quality and capital efficiency, then
selects a basket within aggregate capital, tail-loss, ticker, correlation-group,
position-count, and delta limits. Execution tickets are created from that
selected basket rather than every individually approved candidate.

The `validate` command uses expanding training windows to select a score
threshold, then measures that threshold on the next unseen block of closed
trades. It reports profitable-fold percentage, out-of-sample expectancy, and
threshold stability overall and by strategy. The `drift` command compares the
recent trade window with the older baseline and flags deterioration in
expectancy, win rate, return on risk, score thresholds, and strategy signals.

The `verify` command runs dependency-light health checks: config JSON parsing,
script compilation, unit tests, and optional SQLite integrity. The
`db-maintenance` command performs SQLite `quick_check`, creates timestamped
backups with the SQLite backup API, prunes old backups by retention policy, and
can run `VACUUM` when requested.

The `scenario-stress` command consumes a saved `risk --json` report and applies
auditable market/volatility shocks. Equity positions use beta-adjusted notional
exposure; options use delta, gamma, vega, quantity, and underlying spot when
available, with an explicit value-based fallback for older reports.

## SQLite and broker reconciliation

SQLite is a durable mirror of the existing JSON workflow, so current scripts
remain compatible while the migration proceeds. Schema creation and upgrades
run automatically through `PRAGMA user_version`. Journal writes can dual-write
SQLite with `journal --db state/quant_tools.db ...`.

Broker fill imports use a provider-neutral JSON snapshot:

```json
{
  "positions": [
    {"symbol": "SPY260717P00475000", "type": "OPTION", "quantity": -1}
  ],
  "fills": [
    {
      "fill_id": "broker-fill-123",
      "ticket_id": "QTK-ABC123",
      "ticker": "SPY",
      "strategy": "BULL_PUT",
      "price": 1.12,
      "filled_at": "2026-06-07T10:15:00-04:00"
    }
  ]
}
```

Ticket IDs are matched first. Ticker, strategy, expiration, and strikes are
used only as a fallback. Reconciliation proposes journal updates but never
applies fill data unless `--apply-updates` is passed explicitly, and it never
places orders automatically.

## Project vs skill — what's where

This `quant-tools/` directory is the **project** — the actual code that runs.
The `~/.hermes/skills/mlops/quant-trading-toolkit/` directory is the **skill** —
documentation the agent loads on demand to know how to use the project.

Why split?
- Skills stay small (markdown + references). Easy for the agent to load context.
- Project stays in `~/.openclaw/workspace/` where you can git-track it, back it
  up, and edit the code without touching agent config.
- State files (`positions.json`, `reports/`) live with the project, not the skill.

Don't move the scripts into the skill directory. Don't add new scripts to the
skill directory either. The skill points at the project; the project owns the
runtime state.

## Cron

`hermes cron` job `f834cc501dbf` ("morning-market-brief") calls
`~/.hermes/scripts/cron_brief.py` → `scripts/daily_brief.py --send` every
weekday 8:30 AM ET. Mode: `--no-agent` (pure script, no LLM). Workdir is this
project root. **Schedule is local time** — `30 8 * * 1-5` in the system tz
(America/New_York). DST just works (8:30 AM stays 8:30 AM through EST↔EDT).
Do not bump in November.

## Safety

- **Never place orders without explicit user confirmation.** Show the setup,
  get the green light, then use `~/.hermes/skills/openclaw-imports/public-dot-com/scripts/place_order.py`.
- Account `5OG66124` is BROKERAGE (not paper). All tools handle an empty
  account — risk dashboard uses `--target-watchlist` for demo mode, screener
  still scans from live option chains.
- The risk dashboard's VaR is delta-normal — fine for small shocks, breaks in
  fat-tail events.
- Backtester uses estimated premium and approximate strikes — real-money
  results will typically be 0.5–2% worse per trade due to bid/ask spread,
  slippage, commissions.

## Extending the toolkit

See the `mlops/quant-trading-toolkit` skill → "Extending the toolkit" section
for the recipes: adding a new strategy, vol-regime tool, macro overlay,
earnings backtester, position tracker, or daily-brief section.
