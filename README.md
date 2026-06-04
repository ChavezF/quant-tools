# quant-tools — Public.com options & risk toolkit

Production trading toolkit for the Public.com brokerage account (`5OG66124`).
10 Python scripts that hit the live Public.com API + yfinance, plus state and
reports directories. **No mocks, ever.**

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
cd /home/chavez_f/.openclaw/workspace/quant-tools/scripts
/usr/bin/python3.12 quant.py macro --watchlist SPY QQQ NVDA AAPL MSFT TSLA
/usr/bin/python3.12 quant.py scan --watchlist SPY QQQ NVDA --strategies csp bull_put --min-dte 21 --max-dte 45
/usr/bin/python3.12 quant.py iv-rank --tickers SPY QQQ NVDA AAPL MSFT TSLA AMD
/usr/bin/python3.12 quant.py brief --watchlist SPY QQQ NVDA AAPL MSFT TSLA
```

## Verification

```bash
/usr/bin/python3.12 ~/.hermes/skills/mlops/quant-trading-toolkit/scripts/verify_toolkit.py
```

Runs 11 smoke tests across all tools. Last verified: 2026-06-04, all PASS.

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
project root. **DST-naive** — schedule is `30 12 * * 1-5` UTC in summer, needs
to be bumped to `30 13 * * 1-5` in November.

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
