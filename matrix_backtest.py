"""
Matrix Backtest: 2 stock lists × 3 tolerances × 5 timeframes = 30 combinations
================================================================================
Stock lists : Stock List.csv (752)  vs  ind_nifty500list (504)
Tolerances  : 2%, 5%, 10%
Timeframes  : 1y, 2y, 3y, 4y, 5y  (historical data window for trendline fitting)
              Entry signals only within the respective lookback window

Parameters  : SL=8%, Target=20%, Timeout=180d, FibMin=7, WickTol=5%, MinTouches=3

Run: python matrix_backtest.py
Output: matrix_backtest_results.json + matrix_backtest_report.html
"""

import json, time, sys, os
import pandas as pd
import numpy as np
from scipy.signal import argrelextrema
from datetime import datetime, timedelta
import yfinance as yf
import warnings
warnings.filterwarnings('ignore')

# ── Config ───────────────────────────────────────────────────────────────────
POSITION_SIZE    = 50000.0
SL_PCT           = 8.0
TARGET_PCT       = 20.0
TIMEOUT_DAYS     = 180
CONFLUENCE_MIN   = 7
WICK_TOLERANCE   = 5.0
MIN_WICK_TOUCHES = 3

TOLERANCES  = [2.0, 5.0, 10.0]
TIMEFRAMES  = [1, 2, 3, 4, 5]   # years

STOCK_LISTS = {
    "Stock List (752)": "../Stock List.csv",         # relative to backend_repo
    "Nifty 500 (504)":  "../ind_nifty500list (1).csv",
}

# ── Trendline helpers ─────────────────────────────────────────────────────────

def fit_trendline(monthly_df):
    """Returns (slope, intercept, anchor_idx) or None."""
    if len(monthly_df) < 24:
        return None
    low_prices = monthly_df['Low'].values.flatten()
    order = 10
    tb = argrelextrema(low_prices, np.less, order=order)
    for fo in [8, 6, 5]:
        if len(tb[0]) >= 2:
            break
        tb = argrelextrema(low_prices, np.less, order=fo)
    if len(tb[0]) < 2:
        return None

    num_anchors = min(3, len(tb[0]))
    anchor_idx  = tb[0][-num_anchors:]
    x = [monthly_df['Price_Idx'].iloc[i] for i in anchor_idx]
    y = [low_prices[i] for i in anchor_idx]
    slope, intercept = np.polyfit(x, y, 1)
    if slope <= 0:
        return None

    wick_count = sum(
        1 for i in range(len(monthly_df))
        if abs((low_prices[i] - (slope * monthly_df['Price_Idx'].iloc[i] + intercept))
               / (slope * monthly_df['Price_Idx'].iloc[i] + intercept)) * 100 <= WICK_TOLERANCE
    )
    if wick_count < MIN_WICK_TOUCHES:
        return None

    return slope, intercept, anchor_idx


def calc_fib_score(low_prices, anchor_idx, trigger_price, monthly_df):
    last_idx = anchor_idx[-1]
    last_price = float(low_prices[last_idx])
    data_after = monthly_df.iloc[last_idx:]
    if len(data_after) < 3:
        swing_high = float(monthly_df['High'].max())
    else:
        highs = data_after['High'].values
        maxima = argrelextrema(highs, np.greater, order=3)[0]
        swing_high = float(data_after['High'].iloc[maxima].max()) if len(maxima) > 0 else float(data_after['High'].max())

    fib_range = swing_high - last_price
    if fib_range <= 0:
        return 5
    levels = {
        '38.2': swing_high - fib_range * 0.382,
        '50.0': swing_high - fib_range * 0.500,
        '61.8': swing_high - fib_range * 0.618,
        '78.6': swing_high - fib_range * 0.786,
        '100':  swing_high - fib_range * 1.000,
    }
    min_dist = min(abs((trigger_price - p) / p) * 100 for p in levels.values())
    closest  = min(levels, key=lambda k: abs((trigger_price - levels[k]) / levels[k]) * 100)
    if min_dist <= 1.5:
        score = 10 if min_dist <= 0.3 else (9 if min_dist <= 0.7 else 8)
        if closest == '61.8':
            score = min(10, score + 1)
    else:
        score = 5
    return score


def check_outcome(daily_df, entry_date, entry_price, stop_price, target_price):
    future   = daily_df[daily_df.index > entry_date]
    deadline = entry_date + timedelta(days=TIMEOUT_DAYS)
    for dt, row in future.iterrows():
        if dt > deadline:
            sub = future[future.index <= deadline]
            if sub.empty:
                return 'TIMEOUT', deadline, entry_price, TIMEOUT_DAYS, 0.0
            ep = float(sub['Close'].iloc[-1])
            return 'TIMEOUT', sub.index[-1], ep, (sub.index[-1]-entry_date).days, (ep-entry_price)/entry_price*100
        if float(row['Low']) <= stop_price:
            return 'STOP_LOSS', dt, stop_price, (dt-entry_date).days, -SL_PCT
        if float(row['High']) >= target_price:
            return 'TARGET_HIT', dt, target_price, (dt-entry_date).days, TARGET_PCT
    lp = float(future['Close'].iloc[-1]) if not future.empty else entry_price
    ld = (future.index[-1]-entry_date).days if not future.empty else 0
    return 'OPEN', future.index[-1] if not future.empty else entry_date, lp, ld, (lp-entry_price)/entry_price*100


# ── Scanner for one combo ──────────────────────────────────────────────────────

def run_one(tickers, tolerance, years, label, data_cache):
    cutoff = datetime.now() - timedelta(days=years * 365)
    trades = []

    for ticker in tickers:
        try:
            if ticker not in data_cache:
                continue
            monthly, daily = data_cache[ticker]
            if monthly is None or daily is None:
                continue

            scan_months = monthly[monthly.index >= pd.Timestamp(cutoff)]
            seen_months = set()

            for bar_pos in range(len(scan_months)):
                global_pos = monthly.index.get_loc(scan_months.index[bar_pos])
                hist = monthly.iloc[:global_pos + 1].copy()
                hist['Price_Idx'] = np.arange(len(hist))

                res = fit_trendline(hist)
                if res is None:
                    continue
                slope, intercept, anchor_idx = res
                low_prices = hist['Low'].values.flatten()

                current_idx   = hist['Price_Idx'].iloc[-1]
                current_close = float(hist['Close'].iloc[-1])
                trigger       = slope * current_idx + intercept
                dist_pct      = (current_close - trigger) / trigger * 100

                if abs(dist_pct) > tolerance:
                    continue

                fib_score = calc_fib_score(low_prices, anchor_idx, trigger, hist)
                if fib_score < CONFLUENCE_MIN:
                    continue

                entry_date   = scan_months.index[bar_pos].to_pydatetime()
                month_key    = entry_date.strftime('%Y-%m')
                sig_key      = f"{ticker}_{month_key}"
                if sig_key in seen_months:
                    continue
                seen_months.add(sig_key)

                entry_price  = round(trigger, 2)
                stop_price   = round(entry_price * (1 - SL_PCT / 100), 2)
                target_price = round(entry_price * (1 + TARGET_PCT / 100), 2)
                shares       = int(POSITION_SIZE // entry_price)
                status       = "CRITICAL" if abs(dist_pct) <= 1.0 else "WATCHLIST"

                outcome, exit_date, exit_price, hold_days, pnl_pct = check_outcome(
                    daily, entry_date, entry_price, stop_price, target_price)

                trades.append({
                    "symbol":       ticker.replace(".NS", ""),
                    "entry_date":   entry_date.strftime("%Y-%m-%d"),
                    "exit_date":    exit_date.strftime("%Y-%m-%d") if hasattr(exit_date, 'strftime') else str(exit_date),
                    "entry_price":  entry_price,
                    "exit_price":   round(float(exit_price), 2),
                    "distance_pct": round(abs(dist_pct), 2),
                    "status":       status,
                    "fib_score":    int(fib_score),
                    "outcome":      outcome,
                    "holding_days": int(hold_days),
                    "pnl_pct":      round(float(pnl_pct), 2),
                    "pnl_amount":   round((float(exit_price) - entry_price) * shares, 2),
                })
        except Exception:
            continue

    return build_summary(trades, label, tolerance, years)


def build_summary(trades, label, tolerance, years):
    completed = [t for t in trades if t['outcome'] in ('STOP_LOSS', 'TARGET_HIT', 'TIMEOUT')]
    wins      = [t for t in completed if t['pnl_pct'] > 0]
    losses    = [t for t in completed if t['pnl_pct'] <= 0]
    total_pnl = round(sum(t['pnl_amount'] for t in trades), 2)
    win_rate  = round(len(wins) / len(completed) * 100, 1) if completed else 0
    pf_num    = sum(t['pnl_amount'] for t in wins)
    pf_den    = abs(sum(t['pnl_amount'] for t in losses))
    profit_factor = round(pf_num / pf_den, 2) if pf_den > 0 else (999.0 if pf_num > 0 else 0.0)
    avg_hold  = round(sum(t['holding_days'] for t in trades) / len(trades), 1) if trades else 0
    avg_ret   = round(sum(t['pnl_pct'] for t in trades) / len(trades), 2) if trades else 0

    critical  = [t for t in trades if t['status'] == 'CRITICAL']
    watchlist = [t for t in trades if t['status'] == 'WATCHLIST']

    def status_stats(lst):
        c = [t for t in lst if t['outcome'] in ('STOP_LOSS', 'TARGET_HIT', 'TIMEOUT')]
        w = [t for t in c if t['pnl_pct'] > 0]
        return {
            "trades":   len(lst),
            "win_rate": round(len(w) / len(c) * 100, 1) if c else 0,
            "avg_pnl":  round(sum(t['pnl_pct'] for t in lst) / len(lst), 2) if lst else 0,
        }

    return {
        "label":           label,
        "tolerance_pct":   tolerance,
        "years":           years,
        "total_trades":    len(trades),
        "completed":       len(completed),
        "win_rate":        win_rate,
        "avg_return_pct":  avg_ret,
        "total_pnl":       total_pnl,
        "avg_pnl_per_trade": round(total_pnl / len(trades), 2) if trades else 0,
        "avg_holding_days": avg_hold,
        "profit_factor":   profit_factor,
        "outcome_breakdown": {
            "TARGET_HIT": len([t for t in trades if t['outcome'] == 'TARGET_HIT']),
            "STOP_LOSS":  len([t for t in trades if t['outcome'] == 'STOP_LOSS']),
            "TIMEOUT":    len([t for t in trades if t['outcome'] == 'TIMEOUT']),
            "OPEN":       len([t for t in trades if t['outcome'] == 'OPEN']),
        },
        "by_status": {
            "CRITICAL":  status_stats(critical),
            "WATCHLIST": status_stats(watchlist),
        },
        "trades": trades,
    }


# ── HTML Generator ────────────────────────────────────────────────────────────

def generate_html(all_results, output_path):
    # Build a lookup: list_name -> tolerance -> years -> summary
    def color_win(wr):
        if wr >= 65: return '#22c55e'
        if wr >= 50: return '#eab308'
        return '#ef4444'

    def color_pf(pf):
        if pf >= 3:  return '#22c55e'
        if pf >= 1.5: return '#eab308'
        return '#ef4444'

    # Build tables per list × tolerance
    list_names = list(STOCK_LISTS.keys())
    tol_labels = {2.0: '±2%', 5.0: '±5%', 10.0: '±10%'}

    tables_html = ''
    for list_name in list_names:
        tables_html += f'''
        <div class="section">
            <h2 class="section-title">📋 {list_name}</h2>
            <div class="tab-container">
        '''
        for tol in TOLERANCES:
            tol_key = f"{list_name}_{tol}"
            tables_html += f'<button class="tab-btn" onclick="showTab(\'{tol_key}\')">{tol_labels[tol]}</button>'
        tables_html += '</div>'

        for tol in TOLERANCES:
            tol_key = f"{list_name}_{tol}"
            tables_html += f'<div id="tab_{tol_key}" class="tab-panel" style="display:none;">'
            tables_html += f'''
            <table class="matrix-table">
            <thead>
                <tr>
                    <th>Timeframe</th>
                    <th>Signals</th>
                    <th>Win Rate</th>
                    <th>Avg Return</th>
                    <th>Total P&L</th>
                    <th>Profit Factor</th>
                    <th>Avg Hold</th>
                    <th>🎯 Target</th>
                    <th>🛑 SL</th>
                    <th>⏱ Timeout</th>
                    <th>📂 Open</th>
                    <th>CRITICAL WR</th>
                    <th>WATCHLIST WR</th>
                </tr>
            </thead>
            <tbody>
            '''
            for yr in TIMEFRAMES:
                key = f"{list_name}|{tol}|{yr}"
                r = all_results.get(key)
                if r is None:
                    tables_html += f'<tr><td>{yr}Y</td><td colspan="12">No data</td></tr>'
                    continue
                wr_color  = color_win(r['win_rate'])
                pf_color  = color_pf(r['profit_factor'])
                pnl_color = '#22c55e' if r['total_pnl'] >= 0 else '#ef4444'
                crit_wr   = r['by_status']['CRITICAL']['win_rate']
                watch_wr  = r['by_status']['WATCHLIST']['win_rate']
                ob        = r['outcome_breakdown']

                tables_html += f'''
                <tr>
                    <td class="yr-cell">{yr}Y</td>
                    <td>{r['total_trades']}</td>
                    <td style="color:{wr_color};font-weight:600">{r['win_rate']}%</td>
                    <td style="color:{'#22c55e' if r['avg_return_pct']>=0 else '#ef4444'}">{r['avg_return_pct']}%</td>
                    <td style="color:{pnl_color}">₹{r['total_pnl']:,.0f}</td>
                    <td style="color:{pf_color}">{r['profit_factor']}x</td>
                    <td>{r['avg_holding_days']}d</td>
                    <td style="color:#22c55e">{ob['TARGET_HIT']}</td>
                    <td style="color:#ef4444">{ob['STOP_LOSS']}</td>
                    <td style="color:#eab308">{ob['TIMEOUT']}</td>
                    <td style="color:#94a3b8">{ob['OPEN']}</td>
                    <td style="color:{color_win(crit_wr)}">{crit_wr}% ({r['by_status']['CRITICAL']['trades']})</td>
                    <td style="color:{color_win(watch_wr)}">{watch_wr}% ({r['by_status']['WATCHLIST']['trades']})</td>
                </tr>
                '''
            tables_html += '</tbody></table></div>'
        tables_html += '</div>'  # section

    # Comparison summary table (best config per metric)
    summary_rows = ''
    for list_name in list_names:
        for yr in TIMEFRAMES:
            for tol in TOLERANCES:
                key = f"{list_name}|{tol}|{yr}"
                r = all_results.get(key)
                if not r or r['total_trades'] == 0:
                    continue
                wr_color = color_win(r['win_rate'])
                pnl_color = '#22c55e' if r['total_pnl'] >= 0 else '#ef4444'
                summary_rows += f'''
                <tr>
                    <td>{list_name}</td>
                    <td>{yr}Y</td>
                    <td>{tol_labels[tol]}</td>
                    <td>{r['total_trades']}</td>
                    <td style="color:{wr_color};font-weight:600">{r['win_rate']}%</td>
                    <td style="color:{pnl_color}">₹{r['total_pnl']:,.0f}</td>
                    <td style="color:{color_pf(r['profit_factor'])}">{r['profit_factor']}x</td>
                    <td>{r['avg_return_pct']}%</td>
                    <td>{r['avg_holding_days']}d</td>
                </tr>
                '''

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Trendline Scanner Matrix Backtest</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: #0a0e14; color: #e2e8f0; font-family: 'Segoe UI', sans-serif; padding: 20px; }}
  h1 {{ text-align: center; color: #10b981; margin-bottom: 8px; font-size: 28px; }}
  .subtitle {{ text-align: center; color: #64748b; margin-bottom: 30px; font-size: 13px; }}
  .meta-bar {{ display: flex; gap: 20px; justify-content: center; margin-bottom: 30px; flex-wrap: wrap; }}
  .meta-pill {{ background: #1e2a38; border: 1px solid #2d3a4a; padding: 8px 18px; border-radius: 20px; font-size: 12px; color: #94a3b8; }}
  .meta-pill span {{ color: #10b981; font-weight: 600; }}
  .section {{ background: #111827; border: 1px solid #1f2937; border-radius: 12px; padding: 24px; margin-bottom: 30px; }}
  .section-title {{ font-size: 18px; font-weight: 700; color: #f1f5f9; margin-bottom: 16px; }}
  .tab-container {{ display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; }}
  .tab-btn {{ background: #1e2a38; border: 1px solid #2d3a4a; color: #94a3b8; padding: 8px 20px;
              border-radius: 20px; cursor: pointer; font-size: 13px; transition: all 0.2s; }}
  .tab-btn:hover, .tab-btn.active {{ background: #065f46; border-color: #10b981; color: #10b981; }}
  .tab-panel {{ overflow-x: auto; }}
  .matrix-table {{ width: 100%; border-collapse: collapse; font-size: 13px; min-width: 900px; }}
  .matrix-table th {{ background: #1e2a38; color: #94a3b8; padding: 10px 14px; text-align: left;
                       font-weight: 600; border-bottom: 1px solid #2d3a4a; white-space: nowrap; }}
  .matrix-table td {{ padding: 10px 14px; border-bottom: 1px solid #1a2232; vertical-align: middle; }}
  .matrix-table tr:hover td {{ background: #1a2535; }}
  .yr-cell {{ font-weight: 700; color: #60a5fa; }}
  .summary-section {{ background: #111827; border: 1px solid #1f2937; border-radius: 12px; padding: 24px; margin-bottom: 30px; }}
  .badge-green {{ background: #064e3b; color: #34d399; padding: 2px 10px; border-radius: 12px; font-size: 11px; }}
  .badge-yellow {{ background: #422006; color: #fbbf24; padding: 2px 10px; border-radius: 12px; font-size: 11px; }}
  .badge-red {{ background: #450a0a; color: #f87171; padding: 2px 10px; border-radius: 12px; font-size: 11px; }}
  .generated {{ text-align: center; color: #374151; font-size: 12px; margin-top: 40px; }}
  input.search-box {{ background: #1e2a38; border: 1px solid #2d3a4a; color: #e2e8f0; padding: 8px 14px;
                       border-radius: 8px; font-size: 13px; width: 300px; margin-bottom: 12px; }}
</style>
</head>
<body>

<h1>📈 Trendline Scanner — Matrix Backtest</h1>
<p class="subtitle">2 Stock Lists × 3 Tolerances × 5 Timeframes = 30 Combinations</p>

<div class="meta-bar">
  <div class="meta-pill">SL <span>{SL_PCT}%</span></div>
  <div class="meta-pill">Target <span>{TARGET_PCT}%</span></div>
  <div class="meta-pill">Timeout <span>{TIMEOUT_DAYS}d</span></div>
  <div class="meta-pill">Fib Score Min <span>{CONFLUENCE_MIN}</span></div>
  <div class="meta-pill">Wick Min <span>{MIN_WICK_TOUCHES} touches</span></div>
  <div class="meta-pill">Position Size <span>₹{POSITION_SIZE:,.0f}</span></div>
  <div class="meta-pill">Generated <span>{datetime.now().strftime('%d %b %Y %H:%M')}</span></div>
</div>

{tables_html}

<div class="summary-section">
  <h2 class="section-title">🏆 Full Comparison — All 30 Combinations</h2>
  <input class="search-box" type="text" id="searchBox" onkeyup="filterTable()" placeholder="Filter by list, tolerance...">
  <div style="overflow-x:auto;">
  <table class="matrix-table" id="summaryTable">
    <thead>
      <tr>
        <th>Stock List</th><th>Timeframe</th><th>Tolerance</th>
        <th>Signals</th><th>Win Rate</th><th>Total P&L</th>
        <th>Profit Factor</th><th>Avg Return</th><th>Avg Hold</th>
      </tr>
    </thead>
    <tbody id="summaryBody">
      {summary_rows}
    </tbody>
  </table>
  </div>
</div>

<p class="generated">Generated: {datetime.now().isoformat()} | Stock Yard Trendline Scanner</p>

<script>
function showTab(key) {{
  document.querySelectorAll('.tab-panel').forEach(p => p.style.display = 'none');
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  const panel = document.getElementById('tab_' + key);
  if (panel) panel.style.display = 'block';
  event.target.classList.add('active');
}}

// Auto-show first tab of each section
document.addEventListener('DOMContentLoaded', () => {{
  document.querySelectorAll('.tab-container').forEach(container => {{
    const firstBtn = container.querySelector('.tab-btn');
    if (firstBtn) firstBtn.click();
  }});
}});

function filterTable() {{
  const q = document.getElementById('searchBox').value.toLowerCase();
  document.querySelectorAll('#summaryBody tr').forEach(row => {{
    row.style.display = row.textContent.toLowerCase().includes(q) ? '' : 'none';
  }});
}}
</script>
</body>
</html>'''

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"HTML report saved → {output_path}")


# ── Entry point ───────────────────────────────────────────────────────────────

def load_tickers(csv_path):
    try:
        df = pd.read_csv(csv_path)
        return [str(t).strip() + ".NS" for t in df['Symbol'].tolist() if str(t).strip()]
    except Exception as e:
        print(f"  Failed to load {csv_path}: {e}")
        return []


def prefetch_all(tickers, label):
    """Fetch monthly + daily data for all tickers once. Returns {ticker: (monthly_df, daily_df)}."""
    cache = {}
    print(f"  Pre-fetching data for {len(tickers)} tickers ({label})...")
    for i, ticker in enumerate(tickers, 1):
        try:
            monthly = yf.download(ticker, period="8y", interval="1mo",
                                  auto_adjust=True, progress=False, timeout=15)
            if monthly.empty or len(monthly) < 24:
                cache[ticker] = (None, None)
                continue
            monthly = monthly.dropna()
            monthly['Price_Idx'] = np.arange(len(monthly))

            daily = yf.download(ticker, period="6y", interval="1d",
                                auto_adjust=True, progress=False, timeout=15)
            daily = daily.dropna() if not daily.empty else pd.DataFrame()
            cache[ticker] = (monthly, daily)
        except Exception:
            cache[ticker] = (None, None)
        if i % 100 == 0:
            print(f"    fetched {i}/{len(tickers)}...")
    print(f"  Done pre-fetching. Valid: {sum(1 for v in cache.values() if v[0] is not None)}/{len(tickers)}")
    return cache


if __name__ == "__main__":
    all_results   = {}
    all_trades    = {}
    total_combos  = len(STOCK_LISTS) * len(TOLERANCES) * len(TIMEFRAMES)
    combo_num     = 0

    print(f"\n{'='*70}")
    print(f"  MATRIX BACKTEST: {total_combos} combinations")
    print(f"{'='*70}")

    for list_name, csv_rel_path in STOCK_LISTS.items():
        csv_path = os.path.join(os.path.dirname(__file__), csv_rel_path)
        tickers  = load_tickers(csv_path)
        if not tickers:
            print(f"  SKIP {list_name} — no tickers loaded")
            continue
        print(f"\n  List: {list_name} — {len(tickers)} tickers")

        # Pre-fetch ALL data once for this list — reuse across 15 combos
        data_cache = prefetch_all(tickers, list_name)

        for tol in TOLERANCES:
            for yr in TIMEFRAMES:
                combo_num += 1
                key   = f"{list_name}|{tol}|{yr}"
                label = f"{list_name} | ±{tol}% | {yr}Y"
                print(f"\n  [{combo_num}/{total_combos}] {label}")

                summary = run_one(tickers, tol, yr, label, data_cache)
                all_results[key] = summary
                all_trades[key]  = summary.pop("trades", [])

                print(f"    → {summary['total_trades']} signals | WR {summary['win_rate']}% | "
                      f"PF {summary['profit_factor']}x | P&L ₹{summary['total_pnl']:,.0f}")

    # Save JSON
    output = {
        "generated_at":   datetime.now().isoformat(),
        "parameters": {
            "position_size": POSITION_SIZE, "sl_pct": SL_PCT,
            "target_pct": TARGET_PCT, "timeout_days": TIMEOUT_DAYS,
            "confluence_min": CONFLUENCE_MIN,
        },
        "results": all_results,
        "trades":  all_trades,
    }
    with open("matrix_backtest_results.json", "w") as f:
        json.dump(output, f, indent=2)
    print("\nSaved → matrix_backtest_results.json")

    # Generate HTML
    generate_html(all_results, "matrix_backtest_report.html")
    print("Done.")
