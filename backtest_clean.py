#!/usr/bin/env python3
"""
Clean Trendline Backtest — Nifty 50 | 1 Year
=============================================
EXACT RULES:
  Data    : April 2020 onwards only (post-COVID crash)
  Trendline: Ascending line with >= 3 wick touches (monthly LOW within 5%)
           : NO monthly close bar ever below trendline (unbroken)
  Signal  : Monthly LOW touches within 5% of trendline
  Entry   : Trendline touch price (the LOW at touch, not close)
  SL      : 8% below entry trendline price — daily close check
  Target  : 23% above entry — daily close check
  Timeout : 365 days
"""

import json, time, warnings
import pandas as pd
import numpy as np
from scipy.signal import argrelextrema
from datetime import datetime, timedelta
import yfinance as yf

warnings.filterwarnings('ignore')

# ─── CONFIG ──────────────────────────────────────────────────────────────────
POSITION_SIZE    = 50000.0
SL_PCT           = 8.0    # 8% below trendline touch price
TARGET_PCT       = 23.0   # 23% above entry
TIMEOUT_DAYS     = 365
WICK_PCT         = 5.0    # wick must be within 5% of trendline
MIN_TOUCHES      = 3      # minimum wick touches required
BACKTEST_YEARS   = 1

NIFTY_50 = [
    'ADANIENT.NS','ADANIPORTS.NS','APOLLOHOSP.NS','ASIANPAINT.NS','AXISBANK.NS',
    'BAJAJ-AUTO.NS','BAJAJFINSV.NS','BAJFINANCE.NS','BHARTIARTL.NS','BPCL.NS',
    'BRITANNIA.NS','CIPLA.NS','COALINDIA.NS','DIVISLAB.NS','DRREDDY.NS',
    'EICHERMOT.NS','GRASIM.NS','HCLTECH.NS','HDFCBANK.NS','HDFCLIFE.NS',
    'HEROMOTOCO.NS','HINDALCO.NS','HINDUNILVR.NS','ICICIBANK.NS','INDUSINDBK.NS',
    'INFY.NS','ITC.NS','JSWSTEEL.NS','KOTAKBANK.NS','LT.NS',
    'M&M.NS','MARUTI.NS','NESTLEIND.NS','NTPC.NS','ONGC.NS',
    'POWERGRID.NS','RELIANCE.NS','SBILIFE.NS','SBIN.NS','SHRIRAMFIN.NS',
    'SUNPHARMA.NS','TATACONSUM.NS','TATAMOTORS.NS','TATASTEEL.NS','TCS.NS',
    'TECHM.NS','TITAN.NS','ULTRACEMCO.NS','WIPRO.NS','LTIM.NS'
]


def _scalar(v):
    if hasattr(v,'iloc'): return float(v.iloc[0])
    if hasattr(v,'__len__') and not isinstance(v,str): return float(v.flat[0])
    return float(v)


def find_best_trendline(df):
    """
    Find best ascending trendline on post-COVID data.
    Rules:
      - Only data from April 2020 onwards
      - Must have >= 3 wick touches (LOW within WICK_PCT of line)
      - No monthly CLOSE ever below the trendline (unbroken)
      - Best = most touches + most recent touch + best accuracy to anchors
    Returns: (slope, intercept, ref_df) or None
    """
    # Filter to post-COVID only
    data = df[df.index >= pd.Timestamp('2020-04-01')].copy()
    if len(data) < 12: return None
    data['Idx'] = np.arange(len(data))

    lows   = data['Low'].values.flatten().astype(float)
    closes = data['Close'].values.flatten().astype(float)
    n      = len(data)

    # Find all significant local lows
    anchors = set()
    for order in [10, 8, 6, 5, 4, 3]:
        for idx in argrelextrema(lows, np.less, order=order)[0]:
            anchors.add(int(idx))
    anchors = sorted(anchors)
    if len(anchors) < 2: return None

    best = None
    best_score = -1

    for i in range(len(anchors) - 1):
        a1 = anchors[i]
        if a1 > int(n * 0.85): break
        for j in range(i + 1, len(anchors)):
            a2 = anchors[j]
            if a2 - a1 < 6: continue  # at least 6 months apart

            # Fit line through the two anchor lows
            x = [float(data['Idx'].iloc[a1]), float(data['Idx'].iloc[a2])]
            y = [lows[a1], lows[a2]]
            slope, intercept = np.polyfit(x, y, 1)
            if slope <= 0: continue  # must be ascending

            # RULE: No monthly CLOSE ever went below this trendline
            # (means trendline is unbroken — price always respected it)
            broken = False
            for k in range(n):
                tl = slope * float(data['Idx'].iloc[k]) + intercept
                if tl > 0 and closes[k] < tl * 0.98:  # 2% buffer for wicks
                    broken = True
                    break
            if broken: continue

            # Count wick touches (monthly LOW within WICK_PCT of trendline)
            touches = []
            for k in range(n):
                tl = slope * float(data['Idx'].iloc[k]) + intercept
                if tl > 0 and abs((lows[k] - tl) / tl) * 100 <= WICK_PCT:
                    touches.append(k)
            if len(touches) < MIN_TOUCHES: continue

            # Score: touches + recency of last touch + accuracy to anchors
            tl_a1 = slope * float(data['Idx'].iloc[a1]) + intercept
            tl_a2 = slope * float(data['Idx'].iloc[a2]) + intercept
            acc   = 1.0 / (1.0 + abs((lows[a1]-tl_a1)/lows[a1]) + abs((lows[a2]-tl_a2)/lows[a2]))
            recency = max(touches) / n
            score   = len(touches)*15 + recency*25 + acc*30 + (a2-a1)*0.1

            if score > best_score:
                best_score = score
                best = (slope, intercept, data, a1, a2, len(touches))

    return best


def simulate(daily_df, entry_date, entry_price, sl_price, target_price):
    """SL and Target both on daily CLOSE."""
    try:
        future   = daily_df[daily_df.index > pd.Timestamp(entry_date)]
        deadline = pd.Timestamp(entry_date) + timedelta(days=TIMEOUT_DAYS)
        for dt, row in future.iterrows():
            if dt > deadline: break
            c = _scalar(row['Close'])
            if c <= sl_price:
                d = (dt - pd.Timestamp(entry_date)).days
                return 'STOP_LOSS', dt, round(c,2), d, round((c-entry_price)/entry_price*100,2)
            if c >= target_price:
                d = (dt - pd.Timestamp(entry_date)).days
                return 'TARGET_HIT', dt, target_price, d, TARGET_PCT
        sub = future[future.index <= deadline]
        if sub.empty: return 'TIMEOUT', deadline, entry_price, 0, 0.0
        lp  = _scalar(sub['Close'].iloc[-1])
        d   = (sub.index[-1] - pd.Timestamp(entry_date)).days
        return 'TIMEOUT', sub.index[-1], round(lp,2), d, round((lp-entry_price)/entry_price*100,2)
    except:
        return 'ERROR', entry_date, entry_price, 0, 0.0


def run():
    print(f"\n{'='*65}")
    print(f"  NIFTY 50 — 1-YEAR TRENDLINE BACKTEST [CLEAN RULES]")
    print(f"  Data    : Post-April 2020 only")
    print(f"  Signal  : Monthly LOW touches within {WICK_PCT}% of trendline")
    print(f"  Entry   : Trendline touch price (LOW)")
    print(f"  SL      : {SL_PCT}% below entry — daily close")
    print(f"  Target  : {TARGET_PCT}% above entry — daily close")
    print(f"  Touches : Minimum {MIN_TOUCHES} required | Trendline must be unbroken")
    print(f"  Period  : {(datetime.now()-timedelta(days=365)).strftime('%Y-%m-%d')} to {datetime.now().strftime('%Y-%m-%d')}")
    print(f"{'='*65}\n")

    cutoff     = datetime.now() - timedelta(days=BACKTEST_YEARS*365)
    all_trades = []
    errors     = []

    print("Fetching data...\n")

    for i, ticker in enumerate(NIFTY_50, 1):
        clean = ticker.replace('.NS','')
        print(f"  [{i:2d}/50] {clean:<14}", end='', flush=True)

        try:
            mdf = yf.download(ticker, period='10y', interval='1mo',
                              auto_adjust=True, progress=False, timeout=30)
            if isinstance(mdf.columns, pd.MultiIndex):
                mdf.columns = mdf.columns.get_level_values(0)
            mdf = mdf.dropna()
            if len(mdf) < 12:
                print(f"→ skip"); errors.append(clean); continue

            daily = yf.download(ticker, period='3y', interval='1d',
                                auto_adjust=True, progress=False, timeout=30)
            if isinstance(daily.columns, pd.MultiIndex):
                daily.columns = daily.columns.get_level_values(0)
            daily = daily.dropna()
            if daily.empty:
                print(f"→ no daily"); errors.append(clean); continue

        except:
            print(f"→ ERROR"); errors.append(clean); continue

        # Scan each month in backtest window
        scan_months = mdf[mdf.index >= pd.Timestamp(cutoff)]
        seen, stock_trades = set(), []

        for bar_pos in range(len(scan_months)):
            gpos = mdf.index.get_loc(scan_months.index[bar_pos])
            hist = mdf.iloc[:gpos+1].copy()

            result = find_best_trendline(hist)
            if result is None: continue

            slope, intercept, ref_df, a1, a2, n_touches = result

            # Get current bar values from ref_df (post-2020 data)
            last_idx   = float(ref_df['Idx'].iloc[-1])
            trendline_px = slope * last_idx + intercept
            bar_low    = _scalar(ref_df['Low'].iloc[-1])
            bar_close  = _scalar(ref_df['Close'].iloc[-1])

            # SIGNAL: monthly LOW must touch within WICK_PCT of trendline
            dist_low = (bar_low - trendline_px) / trendline_px * 100
            if abs(dist_low) > WICK_PCT: continue

            # ENTRY: trendline touch price (the LOW value at touch)
            # Use actual bar_low as entry if it's below trendline,
            # else use trendline price
            entry_price  = round(min(bar_low, trendline_px * 1.01), 2)  # at or near trendline
            sl_price     = round(entry_price * (1 - SL_PCT/100), 2)
            target_price = round(entry_price * (1 + TARGET_PCT/100), 2)
            shares       = max(1, int(POSITION_SIZE // entry_price))

            entry_date   = scan_months.index[bar_pos].to_pydatetime()
            mk           = f"{clean}_{entry_date.strftime('%Y-%m')}"
            if mk in seen: continue
            seen.add(mk)

            # Fibonacci level of closest fib to trendline
            fib_level = '—'
            try:
                lows_arr = ref_df['Low'].values.flatten().astype(float)
                last_touch = ref_df.iloc[a2]
                data_after = ref_df.iloc[a2:]
                highs_after = data_after['High'].values.flatten().astype(float)
                mx = argrelextrema(highs_after, np.greater, order=3)[0]
                sh = float(highs_after[mx].max()) if len(mx)>0 else float(highs_after.max())
                lp = float(lows_arr[a2])
                fr = sh - lp
                if fr > 0:
                    fibs = {'23.6%': sh-fr*0.236, '38.2%': sh-fr*0.382,
                            '50.0%': sh-fr*0.500, '61.8%': sh-fr*0.618,
                            '78.6%': sh-fr*0.786, '100.0%': sh-fr*1.000}
                    closest = min(fibs, key=lambda k: abs((trendline_px-fibs[k])/fibs[k]))
                    dist_fib = abs((trendline_px-fibs[closest])/fibs[closest])*100
                    fib_level = f"{closest}({dist_fib:.1f}%)"
            except: pass

            outcome, exit_date, exit_price, days_held, pnl_pct = simulate(
                daily, entry_date, entry_price, sl_price, target_price
            )

            stock_trades.append({
                'symbol':       clean,
                'entry_date':   entry_date.strftime('%Y-%m-%d'),
                'exit_date':    exit_date.strftime('%Y-%m-%d') if hasattr(exit_date,'strftime') else str(exit_date),
                'entry_price':  entry_price,
                'exit_price':   round(float(exit_price),2),
                'sl_price':     sl_price,
                'target_price': target_price,
                'trendline_px': round(trendline_px,2),
                'dist_pct':     round(abs(dist_low),2),
                'wick_touches': n_touches,
                'fib_level':    fib_level,
                'outcome':      outcome,
                'holding_days': int(days_held),
                'pnl_pct':      round(float(pnl_pct),2),
                'pnl_amount':   round((float(exit_price)-entry_price)*shares,2),
                'shares':       shares,
            })

        all_trades.extend(stock_trades)
        print(f"→ {len(stock_trades)} signal(s)" if stock_trades else "→ no signals")

    # ─── SUMMARY ─────────────────────────────────────────────────────────────
    print(f"\n{'='*65}")
    print(f"  RESULTS")
    print(f"{'='*65}\n")

    if not all_trades:
        print("  No trades found."); return

    comp   = [t for t in all_trades if t['outcome'] in ('STOP_LOSS','TARGET_HIT','TIMEOUT')]
    wins   = [t for t in comp if t['pnl_pct'] > 0]
    losses = [t for t in comp if t['pnl_pct'] <= 0]
    hits   = [t for t in all_trades if t['outcome']=='TARGET_HIT']
    sls    = [t for t in all_trades if t['outcome']=='STOP_LOSS']
    tos    = [t for t in all_trades if t['outcome']=='TIMEOUT']

    wr   = round(len(wins)/len(comp)*100,1) if comp else 0
    tpnl = round(sum(t['pnl_amount'] for t in all_trades),2)
    avgr = round(sum(t['pnl_pct'] for t in all_trades)/len(all_trades),2)
    avgh = round(sum(t['holding_days'] for t in all_trades)/len(all_trades),1)
    pg   = sum(t['pnl_amount'] for t in wins)
    pl   = abs(sum(t['pnl_amount'] for t in losses))
    pf   = round(pg/pl,2) if pl>0 else (999.0 if pg>0 else 0.0)

    print(f"  Total signals  : {len(all_trades)}")
    print(f"  Completed      : {len(comp)}")
    print(f"  Win rate       : {wr}%")
    print(f"  Profit factor  : {pf}x")
    print(f"  Total P&L      : Rs{tpnl:,.0f}")
    print(f"  Avg return     : {avgr}%")
    print(f"  Avg hold       : {avgh} days")
    print(f"  Target hits    : {len(hits)}")
    print(f"  Stop losses    : {len(sls)}")
    print(f"  Timeouts       : {len(tos)}")
    print()

    # Per-stock summary
    print(f"  {'Symbol':<14} {'Date':<10} {'Entry':>8} {'TL':>8} {'SL':>8} {'Target':>8} {'Outcome':<12} {'P&L%':>6}")
    print(f"  {'-'*80}")
    for t in sorted(all_trades, key=lambda x: x['entry_date']):
        marker = ' <TARGET' if t['outcome']=='TARGET_HIT' else ' <SL' if t['outcome']=='STOP_LOSS' else ''
        print(f"  {t['symbol']:<14} {t['entry_date']:<10} "
              f"{t['entry_price']:>8,.0f} {t['trendline_px']:>8,.0f} "
              f"{t['sl_price']:>8,.0f} {t['target_price']:>8,.0f} "
              f"{t['outcome']:<12} {t['pnl_pct']:>+6.1f}%{marker}")

    # Save JSON
    out = {
        'generated_at': datetime.now().isoformat(),
        'rules': {
            'data':    'Post-April 2020 only',
            'signal':  f'Monthly LOW within {WICK_PCT}% of trendline',
            'entry':   'Trendline touch price (LOW)',
            'sl':      f'{SL_PCT}% below entry — daily close',
            'target':  f'{TARGET_PCT}% above entry — daily close',
            'touches': f'Minimum {MIN_TOUCHES} wick touches required',
            'rule':    'No monthly close ever below trendline',
        },
        'summary': {
            'total':len(all_trades), 'win_rate':wr, 'profit_factor':pf,
            'total_pnl':tpnl, 'avg_return':avgr, 'avg_hold':avgh,
            'hits':len(hits), 'stop_losses':len(sls), 'timeouts':len(tos),
        },
        'trades': all_trades,
    }
    with open('backtest_clean_results.json','w') as f: json.dump(out,f,indent=2)
    print(f"\n  Saved -> backtest_clean_results.json")

    if errors:
        print(f"\n  Skipped: {', '.join(errors)}")


if __name__ == '__main__':
    run()
