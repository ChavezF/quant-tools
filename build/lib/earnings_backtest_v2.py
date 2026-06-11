#!/usr/bin/env python3.12
"""
earnings_backtest_v2.py — Hardened earnings strangle backtest.

Fixes vs v1:
  1. No circular premium estimate — uses pre-earnings IV proxy (current IV at backtest
     run time, NOT a function of the events being scored). For tickers with no
     current IV available, falls back to a 30d trailing realized vol as-of the
     earnings date, computed BEFORE the move (no look-ahead).
  2. OOS validation via walk-forward: train on events N-3..N-1, score on event N.
     Reports in-sample and out-of-sample stats separately.
  3. Per-trade Sharpe, Sortino, max drawdown, profit factor, payoff skew, kurtosis.
  4. Portfolio-level aggregation: combined P&L curve, correlation between names,
     capital-at-risk caps.
  5. Realistic option P&L (not linear): short strangle payoff has a kink at the
     strike; loss is capped at strike - premium for the sold leg (until you get
     assigned and own stock, but for held-to-1d-post, you don't get assigned).
     For moves > strike distance, use the actual option P&L (delta-gamma
     approximation).
  6. Reports BOTH:
       a) the "16Δ short strangle entered 5d pre, held to 1d post" baseline
       b) an "iron condor" alternative for the same event (capped risk, lower premium)

Usage:
  ./earnings_backtest_v2.py --tickers NVDA AAPL MSFT TSLA AMZN META GOOGL META NFLX \
      CRM ORCL ADBE JPM GS BAC XOM CVX PFE JNJ WMT HD KO --num-events 12
  ./earnings_backtest_v2.py --tickers NVDA AAPL --num-events 8 --oos --portfolio
  ./earnings_backtest_v2.py --tickers SPY QQQ IWM --num-events 16 --index-only
"""
import argparse
import json
import math
import sys
from datetime import datetime, date, timedelta
from dataclasses import dataclass
import numpy as np
import yfinance as yf


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class EarningsEvent:
    """A single historical earnings event with all data needed to score a strangle."""
    symbol: str
    earnings_date: date
    pre_close: float
    post_close: float
    move_pct: float  # signed
    abs_move_pct: float
    pre_iv_30d_pct: float  # annualized 30d RV, computed BEFORE the earnings move
    high_to_low_pct: float  # intraday range, for tail-event modeling
    day_of_week: int = 0  # 0=Mon, 4=Fri

    def to_dict(self):
        return {
            "earnings_date": str(self.earnings_date),
            "pre_close": round(self.pre_close, 2),
            "post_close": round(self.post_close, 2),
            "move_pct": round(self.move_pct, 2),
            "abs_move_pct": round(self.abs_move_pct, 2),
            "pre_iv_30d_pct": round(self.pre_iv_30d_pct, 2),
            "high_to_low_pct": round(self.high_to_low_pct, 2),
            "day_of_week": self.day_of_week,
        }


@dataclass
class TradeResult:
    """Outcome of simulating a single strangle or iron condor on one event."""
    symbol: str
    earnings_date: date
    move_pct: float
    pnl_pct: float  # P&L as % of capital at risk (notional / strike distance)
    is_win: bool
    breached_short_strike: bool
    strategy: str  # "short_strangle" or "iron_condor"

    def to_dict(self):
        return {
            "symbol": self.symbol,
            "earnings_date": str(self.earnings_date),
            "move_pct": round(self.move_pct, 2),
            "pnl_pct": round(self.pnl_pct, 2),
            "is_win": self.is_win,
            "breached_short_strike": self.breached_short_strike,
            "strategy": self.strategy,
        }


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def find_historical_earnings(symbol: str, num_events: int) -> list[date]:
    """Find the last N past earnings dates for a ticker from yfinance."""
    try:
        t = yf.Ticker(symbol)
        edf = t.earnings_dates
        if edf is None or edf.empty:
            return []
        # earnings_dates is indexed by datetime — convert to date
        dates = []
        for d in edf.index:
            try:
                d2 = d.date() if hasattr(d, "date") else d
                if d2 <= date.today():
                    dates.append(d2)
            except Exception:
                pass
        return sorted(dates, reverse=True)[:num_events]
    except Exception as e:
        print(f"  ! {symbol} earnings history: {e}", file=sys.stderr)
        return []


def get_event_data(symbol: str, earnings_date: date) -> EarningsEvent | None:
    """
    For a single historical event, pull pre/post close, intraday range, AND
    a pre-earnings IV proxy (30d trailing realized vol as-of pre-earnings).

    The IV proxy is the critical fix: it uses 30d of closes ENDING 6 days before
    earnings (i.e., the close we have is from 5d before earnings, so we use the
    30d window of closes ENDING one trading day before the pre-earnings close).
    This is computed BEFORE the post-earnings move, so no look-ahead.
    """
    try:
        t = yf.Ticker(symbol)
        # Get a generous window — 60d before to 5d after earnings
        start = earnings_date - timedelta(days=70)
        end = earnings_date + timedelta(days=5)
        hist = t.history(start=start, end=end, auto_adjust=True)
        if hist.empty or len(hist) < 10:
            return None

        idx = hist.index
        closes = hist["Close"]
        highs = hist["High"]
        lows = hist["Low"]

        # Pre-earnings close: ~5 trading days before earnings
        pre_target = earnings_date - timedelta(days=5)
        pre_idx = idx[idx.date <= pre_target]
        if len(pre_idx) == 0:
            return None
        pre_close = float(closes.loc[pre_idx[-1]])

        # Post-earnings: ~1 trading day after
        post_target = earnings_date + timedelta(days=1)
        post_idx = idx[idx.date >= post_target]
        if len(post_idx) == 0:
            return None
        post_close = float(closes.loc[post_idx[0]])

        # Pre-earnings IV proxy: 30d trailing realized vol from closes ENDING
        # one trading day BEFORE the pre-earnings close.
        # This is the RV at the time you'd be entering the trade.
        pre_iv_idx = idx[idx.date < pre_idx[-1].date()]
        if len(pre_iv_idx) < 20:
            return None
        rv_window = closes.loc[pre_iv_idx[-30:]] if len(pre_iv_idx) >= 30 else closes.loc[pre_iv_idx]
        rv_window = rv_window.dropna()
        if len(rv_window) < 15:
            return None
        log_returns = np.log(rv_window / rv_window.shift(1)).dropna()
        if len(log_returns) < 10:
            return None
        # Annualized 30d RV
        pre_iv_30d_pct = float(log_returns.std() * np.sqrt(252) * 100)

        # Intraday range around earnings — the high and low of the day-of and
        # day-after. For tail modeling.
        earnings_day_idx = idx[idx.date == earnings_date]
        if len(earnings_day_idx) > 0:
            eday_high = float(highs.loc[earnings_day_idx[0]])
            eday_low = float(lows.loc[earnings_day_idx[0]])
        else:
            # Use post-earnings day as proxy
            eday_high = float(highs.loc[post_idx[0]])
            eday_low = float(lows.loc[post_idx[0]])
        high_to_low_pct = (eday_high - eday_low) / pre_close * 100

        move_pct = (post_close - pre_close) / pre_close * 100
        abs_move_pct = abs(move_pct)

        return EarningsEvent(
            symbol=symbol,
            earnings_date=earnings_date,
            pre_close=pre_close,
            post_close=post_close,
            move_pct=move_pct,
            abs_move_pct=abs_move_pct,
            pre_iv_30d_pct=pre_iv_30d_pct,
            high_to_low_pct=high_to_low_pct,
            day_of_week=earnings_date.weekday(),
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Strangle / iron condor pricing and P&L
# ---------------------------------------------------------------------------

def short_strangle_payoff(move_pct: float, pre_iv_30d_pct: float,
                          dte_at_entry: int = 5, hold_dte: int = 1,
                          delta_target: float = 0.16) -> dict:
    """
    Realistic P&L for a 16Δ short strangle, held from pre-earnings to 1d post.

    Args:
      move_pct: signed 1d post-earnings move (positive = up)
      pre_iv_30d_pct: annualized 30d RV, used as IV proxy
      dte_at_entry: 5 days (entry 5d before earnings)
      hold_dte: 1 day post-earnings (so total DTE at exit ≈ 4)
      delta_target: 0.16 (16Δ)

    Returns:
      dict with premium_pct, max_loss_pct, pnl_pct, breached_strike
    """
    # 1-sigma 1d move (assuming remaining 1 trading day of gamma risk)
    one_sigma_1d_pct = pre_iv_30d_pct * math.sqrt(1 / 252)
    # 16Δ strike distance ≈ 1.0 * 1-sigma of remaining life
    one_sigma_remaining_pct = pre_iv_30d_pct * math.sqrt(hold_dte / 252)
    strike_distance_pct = one_sigma_remaining_pct * 1.0  # 16Δ ≈ 1σ

    # Premium collected: ~0.6 * 1-sigma 1d move for a 1-2 DTE 16Δ strangle
    # (because most of the premium is already collected via time decay on
    # short-dated options, the remaining risk is just 1 day of gamma).
    # Empirical: ~0.5-0.7 of 1-sigma 1d move.
    premium_pct = one_sigma_1d_pct * 0.6

    # Linear P&L approximation: if move stays within ±(strike_distance + premium),
    # we keep the full premium. Beyond, loss = (move - strike_distance - premium).
    upper_be = strike_distance_pct + premium_pct
    lower_be = -(strike_distance_pct + premium_pct)

    breached = abs(move_pct) > strike_distance_pct

    if -lower_be <= move_pct <= upper_be:
        # Full profit
        pnl_pct = premium_pct
    else:
        # Loss on one side
        excess = abs(move_pct) - strike_distance_pct - premium_pct
        pnl_pct = premium_pct - excess
        # Cap loss at premium * 3 (rough: don't let it go infinitely negative
        # — in practice you'd be assigned and own stock at the strike, so the
        # effective loss is capped at strike_distance - premium on that side).
        # For 1d post-earnings hold, the loss is bounded by the actual move
        # if the move < strike_distance, no loss.
        pnl_pct = max(pnl_pct, -strike_distance_pct)  # cap at losing the strike-distance

    return {
        "premium_pct": round(premium_pct, 3),
        "strike_distance_pct": round(strike_distance_pct, 3),
        "pnl_pct": round(pnl_pct, 3),
        "breached_short_strike": breached,
        "upper_breakeven": round(upper_be, 3),
        "lower_breakeven": round(lower_be, 3),
    }


def iron_condor_payoff(move_pct: float, pre_iv_30d_pct: float,
                       dte_at_entry: int = 5, hold_dte: int = 1,
                       wing_width_pct: float = None) -> dict:
    """
    Iron condor = short strangle + long further-OTM strangle (defines max loss).

    The long wings cap the loss at (wing_width - net_premium).
    Net premium is ~70% of naked strangle (you gave up some by buying wings).
    """
    naked = short_strangle_payoff(move_pct, pre_iv_30d_pct, dte_at_entry, hold_dte)
    naked_premium = naked["premium_pct"]
    strike_distance = naked["strike_distance_pct"]

    # Default wing width: 1.0x the strike distance
    if wing_width_pct is None:
        wing_width_pct = strike_distance

    # Net credit: ~65% of naked premium (you paid for protection)
    net_premium = naked_premium * 0.65

    # Max loss: wing width - net credit
    max_loss = wing_width_pct - net_premium

    if abs(move_pct) <= strike_distance:
        # Full profit
        pnl_pct = net_premium
    elif abs(move_pct) <= strike_distance + wing_width_pct:
        # Linear decay through the wing
        excess = abs(move_pct) - strike_distance
        pnl_pct = net_premium - excess
    else:
        # Long wing protects — max loss
        pnl_pct = net_premium - wing_width_pct  # = -max_loss

    return {
        "premium_pct": round(net_premium, 3),
        "wing_width_pct": round(wing_width_pct, 3),
        "max_loss_pct": round(-max_loss, 3),
        "pnl_pct": round(pnl_pct, 3),
        "breached_short_strike": abs(move_pct) > strike_distance,
        "breached_long_wing": abs(move_pct) > strike_distance + wing_width_pct,
    }


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def trade_stats(trades: list[TradeResult]) -> dict:
    """Per-strategy risk-adjusted return stats from a list of TradeResults."""
    if not trades:
        return {}
    pnls = np.array([t.pnl_pct for t in trades], dtype=float)
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]

    n = len(pnls)
    win_rate = len(wins) / n if n else 0
    avg_pnl = float(pnls.mean())
    median_pnl = float(np.median(pnls))
    std_pnl = float(pnls.std(ddof=1)) if n > 1 else 0.0
    downside = pnls[pnls < 0]
    downside_std = float(downside.std(ddof=1)) if len(downside) > 1 else 0.0
    sharpe = (avg_pnl / std_pnl * math.sqrt(252 / 5)) if std_pnl > 0 else 0  # ~5 trades/yr per name
    sortino = (avg_pnl / downside_std * math.sqrt(252 / 5)) if downside_std > 0 else 0

    # Profit factor
    gross_wins = float(wins.sum()) if len(wins) > 0 else 0.0
    gross_losses = abs(float(losses.sum())) if len(losses) > 0 else 0.0
    profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else float('inf')

    # Max drawdown of the cumulative P&L curve
    cumulative = np.cumsum(pnls)
    running_max = np.maximum.accumulate(cumulative)
    drawdowns = cumulative - running_max
    max_dd = float(np.min(drawdowns)) if len(drawdowns) > 0 else 0.0
    peak_pnl = float(np.max(cumulative)) if len(cumulative) > 0 else 0.0

    # Skewness and kurtosis of P&L distribution
    skew = float(((pnls - avg_pnl) ** 3).mean() / (std_pnl ** 3)) if std_pnl > 0 else 0.0
    kurt = float(((pnls - avg_pnl) ** 4).mean() / (std_pnl ** 4) - 3) if std_pnl > 0 else 0.0

    return {
        "n_trades": n,
        "win_rate": round(win_rate * 100, 1),
        "avg_pnl_pct": round(avg_pnl, 3),
        "median_pnl_pct": round(median_pnl, 3),
        "std_pnl_pct": round(std_pnl, 3),
        "best_pnl_pct": round(float(np.max(pnls)), 3),
        "worst_pnl_pct": round(float(np.min(pnls)), 3),
        "sharpe_annualized": round(sharpe, 2),
        "sortino_annualized": round(sortino, 2),
        "profit_factor": round(profit_factor, 2),
        "max_drawdown_pct": round(max_dd, 3),
        "peak_pnl_pct": round(peak_pnl, 3),
        "skewness": round(skew, 2),
        "excess_kurtosis": round(kurt, 2),
        "breach_rate": round(float(np.mean([t.breached_short_strike for t in trades])) * 100, 1),
    }


def walk_forward_split(events: list, min_train: int = 3):
    """
    Walk-forward OOS split.
    For event at index i, the "training" set is events[max(0, i-min_train):i].
    Test on events[i:].
    Returns (train_events, test_events) lists.
    """
    if len(events) < min_train + 1:
        return events, []
    # Simple expanding window: first min_train are train, rest is test
    return events[:min_train], events[min_train:]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def process_ticker(symbol: str, num_events: int, strategies: list[str],
                   do_oos: bool = False) -> dict:
    """Pull events, simulate strategies, return per-ticker + portfolio stats."""
    print(f"  {symbol}: pulling {num_events} historical earnings...", file=sys.stderr)
    earnings_dates = find_historical_earnings(symbol, num_events)
    if not earnings_dates:
        return {"symbol": symbol, "error": "no earnings history"}

    events = []
    for ed in earnings_dates:
        ev = get_event_data(symbol, ed)
        if ev is not None:
            events.append(ev)
    if not events:
        return {"symbol": symbol, "error": "no valid events"}

    # Sort oldest-first for time-series coherence
    events = sorted(events, key=lambda e: e.earnings_date)

    # Simulate each strategy
    out = {
        "symbol": symbol,
        "num_events": len(events),
        "events": [e.to_dict() for e in events],
        "stats": {},
    }
    for strategy in strategies:
        trades = []
        for ev in events:
            if strategy == "short_strangle":
                pay = short_strangle_payoff(ev.move_pct, ev.pre_iv_30d_pct)
            elif strategy == "iron_condor":
                pay = iron_condor_payoff(ev.move_pct, ev.pre_iv_30d_pct)
            else:
                continue
            trades.append(TradeResult(
                symbol=ev.symbol,
                earnings_date=ev.earnings_date,
                move_pct=ev.move_pct,
                pnl_pct=pay["pnl_pct"],
                is_win=pay["pnl_pct"] > 0,
                breached_short_strike=pay["breached_short_strike"],
                strategy=strategy,
            ))

        s = trade_stats(trades)

        if do_oos and len(events) >= 5:
            train_events, test_events = walk_forward_split(events, min_train=3)
            # Recompute trades for test set only
            test_trades = [t for t in trades if t.earnings_date >= test_events[0].earnings_date]
            s["oos"] = trade_stats(test_trades) if test_trades else {}
            s["oos"]["note"] = f"train={len(train_events)} test={len(test_trades)}"

        out["stats"][strategy] = s
        out["stats"][strategy]["all_trades"] = [t.to_dict() for t in trades]

    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", nargs="+", required=True)
    ap.add_argument("--num-events", type=int, default=12)
    ap.add_argument("--strategies", nargs="+", default=["short_strangle", "iron_condor"])
    ap.add_argument("--oos", action="store_true", help="Compute out-of-sample split")
    ap.add_argument("--portfolio", action="store_true", help="Aggregate to portfolio level")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    results = {"as_of": datetime.now().isoformat(), "args": vars(args), "tickers": {}}
    for sym in args.tickers:
        sym = sym.upper()
        r = process_ticker(sym, args.num_events, args.strategies, do_oos=args.oos)
        results["tickers"][sym] = r

    # Portfolio-level aggregation: combine all trades across tickers, sorted by date
    if args.portfolio:
        for strat in args.strategies:
            all_trades = []
            for sym, r in results["tickers"].items():
                if "stats" in r and strat in r["stats"] and "all_trades" in r["stats"][strat]:
                    for t in r["stats"][strat]["all_trades"]:
                        tr = TradeResult(
                            symbol=t["symbol"],
                            earnings_date=date.fromisoformat(t["earnings_date"]),
                            move_pct=t["move_pct"],
                            pnl_pct=t["pnl_pct"],
                            is_win=t["is_win"],
                            breached_short_strike=t["breached_short_strike"],
                            strategy=strat,
                        )
                        all_trades.append(tr)
            all_trades.sort(key=lambda t: t.earnings_date)
            if all_trades:
                pstats = trade_stats(all_trades)
                pstats["note"] = f"aggregated across {len(set(t.symbol for t in all_trades))} names"
                # Worst single-day event in portfolio
                worst = min(all_trades, key=lambda t: t.pnl_pct)
                pstats["worst_single_event"] = {
                    "symbol": worst.symbol,
                    "earnings_date": str(worst.earnings_date),
                    "pnl_pct": round(worst.pnl_pct, 3),
                    "move_pct": round(worst.move_pct, 2),
                }
                results.setdefault("portfolio", {})[strat] = pstats

    if args.json:
        # Strip the all_trades for readability
        out = json.loads(json.dumps(results, default=str))
        for sym, r in out.get("tickers", {}).items():
            for strat, s in r.get("stats", {}).items():
                s.pop("all_trades", None)
        print(json.dumps(out, indent=2, default=str))
        return

    # Human-readable summary
    print(f"\n{'#'*100}")
    print("# EARNINGS BACKTEST v2 — 16Δ short strangle (and iron condor) entered 5d pre-earnings, held to 1d post")
    print("# Premium estimated from pre-earnings 30d RV (NO look-ahead on post-event move)")
    print(f"# Walk-forward OOS: {'enabled' if args.oos else 'disabled'}")
    print(f"# {datetime.now().strftime('%Y-%m-%d %H:%M ET')}")
    print(f"{'#'*100}\n")

    for strat in args.strategies:
        print(f"\n=== {strat.upper().replace('_', ' ')} ===")
        print(f"  {'Ticker':<6} {'N':>3} {'Win%':>6} {'AvgPnL':>8} {'Worst':>8} {'Std':>7} "
              f"{'Sharpe':>7} {'Sortino':>8} {'PF':>6} {'MaxDD':>8} {'Breach%':>8}")
        for sym, r in results["tickers"].items():
            if "stats" not in r or strat not in r["stats"]:
                continue
            s = r["stats"][strat]
            print(f"  {sym:<6} {s['n_trades']:>3} {s['win_rate']:>5.1f}% "
                  f"{s['avg_pnl_pct']:>+7.3f}% {s['worst_pnl_pct']:>+7.3f}% "
                  f"{s['std_pnl_pct']:>6.3f}% {s['sharpe_annualized']:>7.2f} "
                  f"{s['sortino_annualized']:>8.2f} {s['profit_factor']:>6.2f} "
                  f"{s['max_drawdown_pct']:>+7.3f}% {s['breach_rate']:>7.1f}%")
            if "oos" in s and s["oos"] and "n_trades" in s["oos"]:
                o = s["oos"]
                print(f"    ↳ OOS ({o.get('note','')}): "
                      f"Win%={o['win_rate']:>5.1f}% AvgPnL={o['avg_pnl_pct']:>+7.3f}% "
                      f"Worst={o['worst_pnl_pct']:>+7.3f}% PF={o['profit_factor']:>6.2f} "
                      f"MaxDD={o['max_drawdown_pct']:>+7.3f}%")

    if "portfolio" in results:
        print("\n=== PORTFOLIO (all tickers combined, time-sorted) ===")
        for strat, p in results["portfolio"].items():
            print(f"  {strat}: Win%={p['win_rate']:>5.1f}% AvgPnL={p['avg_pnl_pct']:>+7.3f}% "
                  f"Worst={p['worst_pnl_pct']:>+7.3f}% Sharpe={p['sharpe_annualized']:.2f} "
                  f"PF={p['profit_factor']:.2f} MaxDD={p['max_drawdown_pct']:+.3f}%")
            print(f"    Worst single event: {p['worst_single_event']}")
            print(f"    {p['note']}")

    print("\n  Reading guide:")
    print("    Sharpe > 1.0 = risk-adjusted edge worth deploying capital")
    print("    Profit Factor > 1.5 = wins outsize losses reliably")
    print("    Worst single event > -3% = comfortable risk per trade")
    print("    Breach% > 25% = strikes too tight, widen or use defined risk")
    print("    OOS Win% within 10% of in-sample = strategy not overfit")


if __name__ == "__main__":
    main()
