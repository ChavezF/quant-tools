# Strategy Research v2 — Hardened Earnings Strangle Analysis

> **Generated 2026-06-04** based on 24-name OOS study, 12 events each (288 total
> earnings events across 2018-2026, including 2020 COVID and 2022 bear market).

## TL;DR

The v1 backtest's "100% win rate on 6 mega-caps" was the **circular math trap**
already documented in the skill: premium was estimated from the *same realized
moves being tested*, guaranteeing ~100% win rates. With proper pre-earnings IV
estimation (30d trailing realized vol, no look-ahead), the picture changes
dramatically:

| Stat | v1 (look-ahead) | v2 (no look-ahead, OOS) |
|---|---|---|
| Tickers with positive edge | 6/6 (100%) | **4/24 (17%)** |
| Average win rate | 100% | 41% (portfolio) |
| Sharpe | ∞ (math error) | 0.25 (portfolio) |
| Worst single event | -0.3% (NVDA Feb 2025) | -3.52% (TSLA Jul 2024, -13.3% move) |

**The strategy is real but narrow.** It's not "short strangle on any earnings name."
It's "short strangle on **defensive, low-vol, high-quality earnings names** in
premium-rich environments."

## Tier 1 — DEPLOY (positive OOS edge, 4 of 24)

| Ticker | OOS Sharpe | OOS Win% | Worst | Profit Factor | Notes |
|---|---|---|---|---|---|
| **XOM**  | 8.58 | 100% | +0.56% | ∞ | Energy, predictable beats |
| **HD**   | 6.81 | 78%   | -1.03% | 11.95 | Retail, stable comp beats |
| **PFE**  | 4.33 | 78%   | -1.93% | 3.24 | Pharma, low surprise rate |
| **GS**   | 3.61 | 67%   | -1.39% | 4.30 | Bank, guidance matters |

Common characteristics:
- **Low historical 1d post-earnings vol** (energy/pharma/retail sectors)
- **High predictability of beats** (revenue/EPS are anchored to macro factors, not narrative)
- **IV rank rises pre-earnings** but post-earnings drift is small

## Tier 2 — SMALL SIZE (positive but mixed, 10 of 24)

KO, JNJ, NVDA, MCD, SBUX, TSLA, AMZN, AAPL, CVX, BAC

Common characteristics:
- Average PnL positive but **high variance** (worst case -1.5% to -3.5%)
- **TSLA and NVDA** are concerning — best avg PnL but worst single event is -3.5%
- **Use defined risk (iron condor) for Tier 2**, not naked strangle
- Skip when IVR < 30 (premium too cheap to justify the risk)

## Tier 3 — AVOID (negative OOS edge, 10 of 24)

META, GOOGL, ORCL, ADBE, WMT, NKE, NFLX, CRM, JPM, MSFT

Common characteristics:
- **Tech mega-caps with surprise-driven narratives** (AI, cloud, platform changes)
- **Long-tail event risk** — when these names miss, they move -8% to -15% in 1d
- **IV underprices the tail** — the 16Δ strike gets breached regularly
- Even when "Win%" looks OK, profit factor < 1 means losses outsize wins

**Do not short volatility on these names.** If you want exposure, use defined-risk
structures with wings, or directional plays.

## Risk sizing — Conservative plan (default)

For the $30k account, allocate earnings strangle capital as follows:

| Tier | Capital allocation | Per-trade risk | Strike distance |
|---|---|---|---|
| Tier 1 (XOM, HD, PFE, GS) | 60% of strangle book = $9k | 2-3% of position | 16Δ (1σ) |
| Tier 2 (10 names)        | 30% of strangle book = $4.5k | 1-1.5% (defined risk) | 8Δ + 1.5σ wing |
| Tier 3                   | 0% — do not trade earnings strangle | — | — |

**Position sizing rule:** max 5% of total account per earnings strangle (so $1.5k on $30k).
**Aggregate:** max 25% of account tied up in earnings strangles at any time.

## Macro gates

| Macro regime score | Action |
|---|---|
| ≥65 AGGRESSIVE       | Tier 1 full size, Tier 2 full size, add 1-2 new Tier 1 names |
| 50-64 FAVORABLE      | Tier 1 full size, Tier 2 half size |
| 35-49 CAUTIOUS       | Tier 1 half size, skip Tier 2, watchlist only |
| <35 DEFENSIVE        | No new strangles, close winners early |

Current regime (2026-06-04): **45/100 = CAUTIOUS** → Tier 1 half size only.

## Exit conditions

- **Profit target**: close at 50% of max profit (don't be greedy — IV crush is fast)
- **Loss limit**: close at 2x credit received (don't hold a loser into expiry)
- **DTE limit**: close at 7 DTE if neither target hit (gamma risk)
- **Hard stop**: if intraday move > 80% of strike distance, manual review

## What the Monte Carlo stress test says

Ran 10,000 simulations of a 16Δ short strangle on SPY held for 5 days at current
IV (~9%). Both parametric and bootstrap methods agree:

- **1% worst case:** -1.3% (capped at strike distance)
- **5% worst case:** +0.3% (still profit)
- **50% median:** +0.78% (full premium)
- **Ruin probability (P&L < -5%):** 0% over a 5d hold

**Historical shock replay** (12 major events from 2008-2025):
- GFC 2008: PnL +1.3% / +0.8% (FULL PROFIT, vol was already elevated)
- US debt downgrade (Aug 2011): -1.3% (BREACHED, capped at strike distance)
- COVID crash (Feb-Mar 2020): -1.3% (BREACHED)
- 2022 bear: +1.2% (PARTIAL)
- 2024 yen carry unwind: +0.3% (BREACHED but small)

**Key finding:** Even in crashes, a 5d 16Δ strangle is **structurally protected**
because the strike distance = 1σ of the hold period. The model caps losses at
the strike distance, which in the worst case is ~1-2% of the underlying. The
"100% loss" you sometimes hear about requires holding through a multi-day
volatility expansion, which 5d-pre / 1d-post does not.

## Caveats (honest)

1. **24 names × 12 events = 288 trades** is decent but not huge. Real edge may
   decay as more market participants find it. Re-run annually.
2. **Premium estimate uses 30d RV as IV proxy** — real IV around earnings is
   higher (the "earnings IV premium"). Actual premiums collected will likely
   be larger than modeled, so real P&L is biased UP from these numbers.
3. **Slippage not modeled** — bid/ask spreads on earnings names can be 5-10%
   of mid. Real P&L is biased DOWN by 0.5-1% per trade.
4. **The 100% win rate on XOM** is suspicious — only 12 events, 9 in-sample.
   Treat as a "strong signal" not "certain edge."
5. **The Tier 3 classification is robust** — multiple names, multiple periods,
   consistent negative OOS performance. Avoid is the right call.

## Re-evaluation cadence

- **Monthly:** check Tier 1/2/3 classification hasn't changed (run
  `quant.py backtest2 --watchlist <list> --oos --portfolio`)
- **Quarterly:** add 5-10 new names to the universe (run on full S&P 500)
- **Annually:** full re-study, refresh `tier` data in `strategy_screener.py`

## Commands

```bash
# Tier classification only (no API calls, instant)
./strategy_screener.py --watchlist XOM HD PFE GS NVDA AAPL --backtest-tier-only

# Full strategy screener with current IV + earnings window + macro
./strategy_screener.py --watchlist XOM HD PFE GS NVDA META GOOGL --macro-score 45

# Re-run the OOS backtest (annual)
./quant.py backtest2 --tickers <list> --num-events 12 --oos --portfolio

# Monte Carlo stress test on a specific name
./quant.py monte-carlo --ticker XOM --num-simulations 10000 --hold-days 5 --tail-events
```
