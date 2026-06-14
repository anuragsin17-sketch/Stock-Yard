"""
Backtest Comparison: 2% vs 5% touch_tolerance
==============================================
Simulates what signals would have fired historically and tracks outcomes.

Entry logic (per monthly bar, scanning backward):
  - Fit trendline on data UP TO that bar (no lookahead)
  - If current bar close is within tolerance% of trendline → entry signal
  - Entry price = trendline value at that bar
  - Stop loss  = entry * (1 - sl_pct/100)
  - Target     = entry * (1 + target_pct/100)
  - Outcome checked on subsequent bars (daily resolution)
  - Timeout after 180 calendar days → exit at price on timeout date

For the 5% config:
  - Stocks between 1%-5% away → WATCHLIST (shown in list, no Telegram)
  - Stocks within 1%         → CRITICAL_TOUCH (Telegram alert + entry)
  For fair comparison we enter ALL qualifying stocks in both configs.

Run: python backtest_compare.py
Output: backtest_compare_results.json  +  printed summary table
"""

import json
import time
import pandas as pd
import numpy as np
from scipy.signal import argrelextrema
from datetime import datetime, timedelta
import yfinance as yf
import warnings
warnings.filterwarnings('ignore')

# ── Parameters ──────────────────────────────────────────────────────────────
BACKTEST_YEARS   = 2          # last 2 years
POSITION_SIZE    = 50000.0
SL_PCT           = 8.0
TARGET_PCT       = 20.0
TIMEOUT_DAYS     = 180
CONFLUENCE_MIN   = 7          # same for both configs
WICK_TOLERANCE   = 5.0        # same for both configs
MIN_WICK_TOUCHES = 3          # same for both configs

CONFIGS = {
    "2pct_baseline": {"touch_tolerance": 2.0, "label": "±2% (current)"},
    "5pct_new":      {"touch_tolerance": 5.0, "label": "±5% (proposed)"},
}

# ── Helpers ──────────────────────────────────────────────────────────────────

def load_tickers(csv_path="Stock List.csv", limit=None):
    try:
        df = pd.read_csv(csv_path)
        tickers = [str(t).strip() + ".NS" for t in df['Symbol'].tolist() if str(t).strip()]
        return tickers[:limit] if limit else tickers
    except Exception:
        return ["SBIN.NS", "INFY.NS", "RELIANCE.NS", "HDFCBANK.NS", "TCS.NS",
                "ICICIBANK.NS", "KOTAKBANK.NS", "LT.NS", "TITAN.NS", "AXISBANK.NS"]


def get_sector_order(ticker):
    banking = ['SBIN', 'HDFCBANK', 'ICICIBANK', 'AXISBANK', 'KOTAKBANK',
               'INDUSINDBK', 'FEDERALBNK', 'BANDHANBNK', 'RBLBANK', 'IDFCFIRSTB']
    return 6 if any(b in ticker.upper() for b in banking) else 10


def fit_trendline(monthly_df):
    """Fit ascending trendline on monthly data slice. Returns (slope, intercept) or None."""
    if len(monthly_df) < 24:
        return None
    low_prices = monthly_df['Low'].values.flatten()
    base_order = 10
    touchbacks = argrelextrema(low_prices, np.less, order=base_order)
    for fo in [8, 6, 5]:
        if len(touchbacks[0]) >= 2:
            break
        touchbacks = argrelextrema(low_prices, np.less, order=fo)
    if len(touchbacks[0]) < 2:
        return None

    num_anchors = min(3, len(touchbacks[0]))
    anchor_idx  = touchbacks[0][-num_anchors:]
    x = [monthly_df['Price_Idx'].iloc[i] for i in anchor_idx]
    y = [low_prices[i] for i in anchor_idx]
    slope, intercept = np.polyfit(x, y, 1)
    if slope <= 0:
        return None

    # Count wick touches
    wick_count = 0
    for i in range(len(monthly_df)):
        tl_val = slope * monthly_df['Price_Idx'].iloc[i] + intercept
        dist   = abs((low_prices[i] - tl_val) / tl_val) * 100
        if dist <= WICK_TOLERANCE:
            wick_count += 1
    if wick_count < MIN_WICK_TOUCHES:
        return None

    return slope, intercept, anchor_idx, touchbacks


def calc_fib_score(low_prices, anchor_idx, trigger_price, monthly_df):
    """Return (confluence_score, swing_high) using same logic as MacroInstitutionalEngine."""
    last_touch_idx  = anchor_idx[-1]
    last_touch_price = float(low_prices[last_touch_idx])
    # Swing high after last touch
    data_after = monthly_df.iloc[last_touch_idx:]
    if len(data_after) < 3:
        swing_high = float(monthly_df['High'].max())
    else:
        highs = data_after['High'].values
        maxima = argrelextrema(highs, np.greater, order=3)[0]
        swing_high = float(data_after['High'].iloc[maxima].max()) if len(maxima) > 0 else float(data_after['High'].max())

    fib_range = swing_high - last_touch_price
    if fib_range <= 0:
        return 5, swing_high

    fib_levels = {
        '38.2%': swing_high - fib_range * 0.382,
        '50.0%': swing_high - fib_range * 0.500,
        '61.8%': swing_high - fib_range * 0.618,
        '78.6%': swing_high - fib_range * 0.786,
        '100.0%': swing_high - fib_range * 1.000,
    }
    min_dist  = min(abs((trigger_price - p) / p) * 100 for p in fib_levels.values())
    closest   = min(fib_levels, key=lambda k: abs((trigger_price - fib_levels[k]) / fib_levels[k]) * 100)

    if min_dist <= 1.5:
        score = 10 if min_dist <= 0.3 else (9 if min_dist <= 0.7 else 8)
        if closest == '61.8%':
            score = min(10, score + 1)
    else:
        score = 5
    return score, swing_high


def check_outcome(daily_df, entry_date, entry_price, stop_price, target_price):
    """
    Check daily OHLC after entry_date for SL/target hit or timeout.
    Returns (outcome, exit_date, exit_price, holding_days, pnl_pct).
    """
    future = daily_df[daily_df.index > entry_date].copy()
    deadline = entry_date + timedelta(days=TIMEOUT_DAYS)

    for dt, row in future.iterrows():
        if dt > deadline:
            # Timeout — exit at close on deadline
            timeout_row = future[future.index <= deadline]
            if timeout_row.empty:
                return 'TIMEOUT', deadline, entry_price, TIMEOUT_DAYS, 0.0
            exit_px  = float(timeout_row['Close'].iloc[-1])
            h_days   = (timeout_row.index[-1] - entry_date).days
            pnl      = (exit_px - entry_price) / entry_price * 100
            return 'TIMEOUT', timeout_row.index[-1], exit_px, h_days, pnl

        low  = float(row['Low'])
        high = float(row['High'])

        if low <= stop_price:
            h_days = (dt - entry_date).days
            return 'STOP_LOSS', dt, stop_price, h_days, -SL_PCT

        if high >= target_price:
            h_days = (dt - entry_date).days
            return 'TARGET_HIT', dt, target_price, h_days, TARGET_PCT

    # Still open
    last_px  = float(future['Close'].iloc[-1]) if not future.empty else entry_price
    h_days   = (future.index[-1] - entry_date).days if not future.empty else 0
    pnl      = (last_px - entry_price) / entry_price * 100
    return 'OPEN', future.index[-1] if not future.empty else entry_date, last_px, h_days, pnl


# ── Main backtest ─────────────────────────────────────────────────────────────

def run_backtest(tickers, tolerance, label):
    backtest_start = datetime.now() - timedelta(days=BACKTEST_YEARS * 365)
    trades = []
    processed = skipped = signals_found = 0

    print(f"\n{'='*60}")
    print(f"Running: {label}  (tolerance={tolerance}%)")
    print(f"{'='*60}")

    for i, ticker in enumerate(tickers, 1):
        try:
            # Fetch full history (8y monthly for trendline + daily for outcome)
            monthly = yf.download(ticker, period="8y", interval="1mo",
                                  auto_adjust=True, progress=False)
            if monthly.empty or len(monthly) < 24:
                skipped += 1
                continue
            monthly = monthly.dropna()
            monthly['Price_Idx'] = np.arange(len(monthly))

            daily = yf.download(ticker, period="3y", interval="1d",
                                auto_adjust=True, progress=False)
            if daily.empty:
                skipped += 1
                continue
            daily = daily.dropna()

            # Identify monthly bars that fall within the 2-year backtest window
            scan_months = monthly[monthly.index >= pd.Timestamp(backtest_start)]

            for bar_pos in range(len(scan_months)):
                # Use data only up to this bar (no lookahead)
                global_pos = monthly.index.get_loc(scan_months.index[bar_pos])
                hist_slice = monthly.iloc[:global_pos + 1].copy()
                hist_slice['Price_Idx'] = np.arange(len(hist_slice))

                result = fit_trendline(hist_slice)
                if result is None:
                    continue

                slope, intercept, anchor_idx, _ = result
                low_prices = hist_slice['Low'].values.flatten()

                current_bar_idx   = hist_slice['Price_Idx'].iloc[-1]
                current_close     = float(hist_slice['Close'].iloc[-1])
                trigger_price     = slope * current_bar_idx + intercept
                pct_distance      = (current_close - trigger_price) / trigger_price * 100

                # Apply tolerance filter
                if abs(pct_distance) > tolerance:
                    continue

                # Fibonacci confluence
                fib_score, _ = calc_fib_score(low_prices, anchor_idx, trigger_price, hist_slice)
                if fib_score < CONFLUENCE_MIN:
                    continue

                # Build entry
                entry_date  = scan_months.index[bar_pos].to_pydatetime()
                entry_price = round(trigger_price, 2)
                stop_price  = round(entry_price * (1 - SL_PCT / 100), 2)
                target_price = round(entry_price * (1 + TARGET_PCT / 100), 2)
                shares      = int(POSITION_SIZE // entry_price)

                # Status label
                if abs(pct_distance) <= 1.0:
                    status = "CRITICAL_TOUCH"
                else:
                    status = "WATCHLIST"

                outcome, exit_date, exit_price, holding_days, pnl_pct = check_outcome(
                    daily, entry_date, entry_price, stop_price, target_price
                )

                pnl_amount = round((exit_price - entry_price) * shares, 2)
                trades.append({
                    "symbol":       ticker.replace(".NS", ""),
                    "entry_date":   entry_date.strftime("%Y-%m-%d"),
                    "exit_date":    exit_date.strftime("%Y-%m-%d") if hasattr(exit_date, 'strftime') else str(exit_date),
                    "entry_price":  entry_price,
                    "exit_price":   round(exit_price, 2),
                    "stop_loss":    stop_price,
                    "target":       target_price,
                    "shares":       shares,
                    "distance_pct": round(abs(pct_distance), 2),
                    "status":       status,
                    "fib_score":    fib_score,
                    "outcome":      outcome,
                    "holding_days": holding_days,
                    "pnl_pct":      round(pnl_pct, 2),
                    "pnl_amount":   pnl_amount,
                })
                signals_found += 1

            processed += 1
            if i % 50 == 0:
                print(f"  [{i}/{len(tickers)}] processed={processed} signals={signals_found}")

        except Exception as e:
            skipped += 1
            continue

    # Build summary
    completed = [t for t in trades if t['outcome'] in ('STOP_LOSS', 'TARGET_HIT', 'TIMEOUT')]
    wins   = [t for t in completed if t['pnl_pct'] > 0]
    losses = [t for t in completed if t['pnl_pct'] <= 0]
    target_hits = [t for t in trades if t['outcome'] == 'TARGET_HIT']
    stop_losses = [t for t in trades if t['outcome'] == 'STOP_LOSS']
    timeouts    = [t for t in trades if t['outcome'] == 'TIMEOUT']
    open_trades = [t for t in trades if t['outcome'] == 'OPEN']

    critical_trades  = [t for t in trades if t['status'] == 'CRITICAL_TOUCH']
    watchlist_trades = [t for t in trades if t['status'] == 'WATCHLIST']

    def safe_avg(lst, key):
        return round(sum(t[key] for t in lst) / len(lst), 2) if lst else 0

    total_pnl    = round(sum(t['pnl_amount'] for t in trades), 2)
    win_rate     = round(len(wins) / len(completed) * 100, 1) if completed else 0
    avg_return   = safe_avg(trades, 'pnl_pct')
    avg_holding  = safe_avg(trades, 'holding_days')
    profit_factor = (
        round(sum(t['pnl_amount'] for t in wins) / abs(sum(t['pnl_amount'] for t in losses)), 2)
        if losses and sum(t['pnl_amount'] for t in losses) != 0 else float('inf')
    )

    summary = {
        "label":          label,
        "tolerance_pct":  tolerance,
        "total_trades":   len(trades),
        "completed":      len(completed),
        "winning":        len(wins),
        "losing":         len(losses),
        "open_trades":    len(open_trades),
        "win_rate":       win_rate,
        "avg_return_pct": avg_return,
        "total_pnl":      total_pnl,
        "avg_pnl_per_trade": round(total_pnl / len(trades), 2) if trades else 0,
        "avg_holding_days":  avg_holding,
        "profit_factor":     profit_factor,
        "outcome_breakdown": {
            "TARGET_HIT": len(target_hits),
            "STOP_LOSS":  len(stop_losses),
            "TIMEOUT":    len(timeouts),
            "OPEN":       len(open_trades),
        },
        "by_status": {
            "CRITICAL_TOUCH": {
                "trades":   len(critical_trades),
                "win_rate": round(len([t for t in critical_trades if t['pnl_pct'] > 0 and t['outcome'] != 'OPEN'])
                                  / max(len([t for t in critical_trades if t['outcome'] != 'OPEN']), 1) * 100, 1),
                "avg_pnl":  safe_avg(critical_trades, 'pnl_pct'),
            },
            "WATCHLIST": {
                "trades":   len(watchlist_trades),
                "win_rate": round(len([t for t in watchlist_trades if t['pnl_pct'] > 0 and t['outcome'] != 'OPEN'])
                                  / max(len([t for t in watchlist_trades if t['outcome'] != 'OPEN']), 1) * 100, 1),
                "avg_pnl":  safe_avg(watchlist_trades, 'pnl_pct'),
            }
        }
    }

    return summary, trades


def print_comparison(results):
    print("\n")
    print("=" * 72)
    print("  BACKTEST COMPARISON: Last 2 Years")
    print("=" * 72)
    print(f"{'Metric':<32} {'±2% (current)':>18} {'±5% (proposed)':>18}")
    print("-" * 72)

    metrics = [
        ("Total Signals",        "total_trades",      ""),
        ("Completed Trades",     "completed",         ""),
        ("Win Rate",             "win_rate",          "%"),
        ("Avg Return/Trade",     "avg_return_pct",    "%"),
        ("Total P&L",            "total_pnl",         "₹"),
        ("Avg P&L/Trade",        "avg_pnl_per_trade", "₹"),
        ("Avg Holding Days",     "avg_holding_days",  " days"),
        ("Profit Factor",        "profit_factor",     "x"),
        ("TARGET_HIT",           None,                ""),
        ("STOP_LOSS",            None,                ""),
        ("TIMEOUT",              None,                ""),
        ("OPEN (active)",        None,                ""),
    ]

    r2 = results["2pct_baseline"]["summary"]
    r5 = results["5pct_new"]["summary"]

    for label, key, unit in metrics:
        if key is None:
            outcome_key = label.split(" ")[0]
            v2 = r2["outcome_breakdown"].get(outcome_key, 0)
            v5 = r5["outcome_breakdown"].get(outcome_key, 0)
            print(f"  {label:<30} {str(v2)+unit:>18} {str(v5)+unit:>18}")
        else:
            v2 = r2[key]
            v5 = r5[key]
            fmt = lambda v: f"{'∞' if v == float('inf') else v}{unit}"
            print(f"  {label:<30} {fmt(v2):>18} {fmt(v5):>18}")

    print("-" * 72)
    print("\n  By Entry Status:")
    for status in ["CRITICAL_TOUCH", "WATCHLIST"]:
        s2 = r2["by_status"][status]
        s5 = r5["by_status"][status]
        print(f"\n  {status}")
        print(f"    {'Trades':<28} {s2['trades']:>18} {s5['trades']:>18}")
        print(f"    {'Win Rate':<28} {str(s2['win_rate'])+'%':>18} {str(s5['win_rate'])+'%':>18}")
        print(f"    {'Avg P&L':<28} {str(s2['avg_pnl'])+'%':>18} {str(s5['avg_pnl'])+'%':>18}")

    print("\n" + "=" * 72)


if __name__ == "__main__":
    import sys

    # Use a subset for quick testing, or full list
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    tickers = load_tickers(limit=limit)
    print(f"Loaded {len(tickers)} tickers")
    print(f"Backtest period: last {BACKTEST_YEARS} years")
    print(f"Parameters: SL={SL_PCT}%, Target={TARGET_PCT}%, Timeout={TIMEOUT_DAYS}d, FibMin={CONFLUENCE_MIN}")

    results = {}
    for config_key, config in CONFIGS.items():
        summary, trades = run_backtest(tickers, config["touch_tolerance"], config["label"])
        results[config_key] = {"summary": summary, "trades": trades}
        print(f"\n  Done: {config['label']} → {len(trades)} trades, win rate {summary['win_rate']}%")

    print_comparison(results)

    # Save results
    output = {
        "generated_at":    datetime.now().isoformat(),
        "backtest_years":  BACKTEST_YEARS,
        "tickers_tested":  len(tickers),
        "parameters": {
            "position_size":  POSITION_SIZE,
            "stop_loss_pct":  SL_PCT,
            "target_pct":     TARGET_PCT,
            "timeout_days":   TIMEOUT_DAYS,
            "confluence_min": CONFLUENCE_MIN,
        },
        "configs": {k: v["summary"] for k, v in results.items()},
        "trades": {k: v["trades"] for k, v in results.items()},
    }

    with open("backtest_compare_results.json", "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nSaved → backtest_compare_results.json")
