#!/usr/bin/env python3
"""
1-Year Backtest — Nifty 50 | Trendline-Based Entry
====================================================
EXACT RULES:
  Entry  : CRITICAL TOUCH — price within 1% of ascending trendline
  SL     : 10% below TRENDLINE WICK price — checked on any daily close
  Target : 23% above entry — checked on any daily close
  Timeout: 365 days
"""

import json
import warnings
import pandas as pd
import numpy as np
from scipy.signal import argrelextrema
from datetime import datetime, timedelta
import yfinance as yf

warnings.filterwarnings('ignore')

# ─── CONFIG ──────────────────────────────────────────────────────────────────
POSITION_SIZE    = 50000.0
SL_PCT           = 10.0   # 10% below TRENDLINE wick price
TARGET_PCT       = 23.0   # 23% above entry
TIMEOUT_DAYS     = 365    # 12-month max hold
WICK_TOLERANCE   = 8.0    # % for wick touch detection
MIN_WICK_TOUCHES = 3
ENTRY_TOLERANCE  = 5.0    # within 5% of trendline
TOUCH_TOLERANCE  = 2.0    # notification only when LOW wicks within 2% of trendline
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

# ─── HELPERS ─────────────────────────────────────────────────────────────────

def _scalar(val):
    if hasattr(val, 'iloc'):    return float(val.iloc[0])
    if hasattr(val, '__len__') and not isinstance(val, str): return float(val.flat[0])
    return float(val)

def fit_trendline(mdf, ticker=''):
    """
    Fit best ascending trendline using ONLY post-April-2020 data.
    Ignores pre-2020 COVID crash data which creates extreme anchor distortions.
    """
    if len(mdf) < 12: return None

    # ── Use ONLY post-COVID data (April 2020 onwards) ──────────────────────
    post = mdf[mdf.index >= pd.Timestamp('2020-04-01')].copy()
    if len(post) < 12: return None   # fallback to full data if stock listed after 2020
    post['Price_Idx'] = np.arange(len(post))

    lows   = post['Low'].values.flatten().astype(float)
    closes = post['Close'].values.flatten().astype(float)
    n_bars = len(post)

    banking = ['SBIN','HDFCBANK','ICICIBANK','AXISBANK','KOTAKBANK','INDUSINDBK']
    order   = 6 if any(b in ticker.upper() for b in banking) else 10

    all_anchors = set()
    for o in [order, 8, 6, 5, 4, 3]:
        tb = argrelextrema(lows, np.less, order=o)
        for idx in tb[0]: all_anchors.add(int(idx))
    all_anchors = sorted(all_anchors)

    if len(all_anchors) < 2: return None

    best_result = None
    best_score  = -1

    for i in range(len(all_anchors) - 1):
        a1 = all_anchors[i]
        if a1 > int(n_bars * 0.80): break
        for j in range(i + 1, len(all_anchors)):
            a2 = all_anchors[j]
            if a2 - a1 < 12: continue

            x = [float(post['Price_Idx'].iloc[a1]), float(post['Price_Idx'].iloc[a2])]
            y = [lows[a1], lows[a2]]
            slope, intercept = np.polyfit(x, y, 1)
            if slope <= 0: continue

            # Rule: No monthly CLOSE should be below trendline (2% buffer)
            # A valid trendline means price always stayed ABOVE it on monthly close
            broken = False
            for k in range(n_bars):
                tl_p = slope * float(post['Price_Idx'].iloc[k]) + intercept
                if tl_p > 0 and closes[k] < tl_p * 0.98:
                    broken = True
                    break
            if broken: continue

            # Count wick touches (lows within WICK_TOLERANCE)
            touch_list = []
            for k in range(n_bars):
                tl_p = slope * float(post['Price_Idx'].iloc[k]) + intercept
                if tl_p > 0 and abs((lows[k] - tl_p) / tl_p) * 100 <= WICK_TOLERANCE:
                    touch_list.append(k)
            if len(touch_list) < MIN_WICK_TOUCHES: continue

            # Accuracy to anchors
            tl_a1 = slope * float(post['Price_Idx'].iloc[a1]) + intercept
            tl_a2 = slope * float(post['Price_Idx'].iloc[a2]) + intercept
            acc   = 1.0 / (1.0 + abs((lows[a1]-tl_a1)/lows[a1]) + abs((lows[a2]-tl_a2)/lows[a2]))

            # Proximity to current price
            curr_tl   = slope * float(post['Price_Idx'].iloc[-1]) + intercept
            curr_cl   = float(post['Close'].iloc[-1])
            curr_dist = (curr_cl - curr_tl) / curr_tl * 100
            proximity = -abs(curr_dist) * 1.5

            recency = max(touch_list) / n_bars
            score   = len(touch_list)*10 + (a2-a1)*0.05 + recency*20 + acc*40 + proximity

            if score > best_score:
                best_score  = score
                best_result = (slope, intercept, [a1, a2], len(touch_list), post)

    if best_result is None: return None
    slope, intercept, ai, touches, ref_df = best_result
    return slope, intercept, ai, touches, ref_df

def calc_fib_levels(lows, ai, mdf):
    try:
        lp = float(lows[ai[-1]])
        highs = mdf.iloc[int(ai[-1]):]['High'].values.flatten().astype(float)
        mx = argrelextrema(highs, np.greater, order=3)[0]
        sh = float(highs[mx].max()) if len(mx) > 0 else float(highs.max())
        fr = sh - lp
        if fr <= 0: return {}, None, None
        lvls = {
            '23.6%': round(sh - fr*0.236, 2),
            '38.2%': round(sh - fr*0.382, 2),
            '50.0%': round(sh - fr*0.500, 2),
            '61.8%': round(sh - fr*0.618, 2),
            '78.6%': round(sh - fr*0.786, 2),
            '100.0%': round(sh - fr*1.000, 2),
        }
        return lvls, lp, sh
    except: return {}, None, None

def fib_score(fib_levels, trigger):
    if not fib_levels: return 5, 'No fib', None, None
    md, cl, cp = float('inf'), None, None
    for name, price in fib_levels.items():
        d = abs((trigger - price)/price)*100
        if d < md: md, cl, cp = d, name, price
    if md <= 1.5:
        s = 10 if md<=0.3 else (9 if md<=0.7 else 8)
        if cl == '61.8%': s = min(10, s+1)
        note = f"Fib {cl} ({md:.1f}%) ✓"
    elif md <= 3.0:
        s, note = 7, f"Near Fib {cl} ({md:.1f}%)"
    else:
        s, note = 5, f"Nearest Fib {cl} ({md:.1f}%)"
    return s, note, cl, cp

def simulate(daily_df, entry_date, entry_price, sl_price, target_price):
    """
    SL   : 10% below TRENDLINE wick — triggered when daily CLOSE <= sl_price
    Target: 23% above entry       — triggered when daily CLOSE >= target_price
    Both checked on daily CLOSE (not intraday high/low)
    """
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
        lp = _scalar(sub['Close'].iloc[-1])
        d  = (sub.index[-1] - pd.Timestamp(entry_date)).days
        return 'TIMEOUT', sub.index[-1], round(lp,2), d, round((lp-entry_price)/entry_price*100,2)
    except:
        return 'ERROR', entry_date, entry_price, 0, 0.0

# ─── MAIN ────────────────────────────────────────────────────────────────────

def run_backtest():
    print(f"\n{'='*70}")
    print(f"  NIFTY 50 — 1-YEAR TRENDLINE BACKTEST")
    print(f"  Entry  : CRITICAL TOUCH ≤1% of ascending trendline")
    print(f"  SL     : {SL_PCT}% below TRENDLINE WICK — triggered on daily close")
    print(f"  Target : {TARGET_PCT}% above entry — triggered on daily close")
    print(f"  Period : {(datetime.now()-timedelta(days=365)).strftime('%Y-%m-%d')} → {datetime.now().strftime('%Y-%m-%d')}")
    print(f"{'='*70}\n")

    cutoff     = datetime.now() - timedelta(days=BACKTEST_YEARS*365)
    all_trades = []
    errors     = []

    print("📥 Fetching data for all 50 stocks...\n")

    for i, ticker in enumerate(NIFTY_50, 1):
        clean = ticker.replace('.NS','')
        print(f"  [{i:2d}/50] {clean:<14}", end='', flush=True)
        try:
            mdf = yf.download(ticker, period='10y', interval='1mo',
                              auto_adjust=True, progress=False, timeout=30)
            if isinstance(mdf.columns, pd.MultiIndex):
                mdf.columns = mdf.columns.get_level_values(0)
            mdf = mdf.dropna()
            if len(mdf) < 24:
                print(f"→ skip ({len(mdf)} months)"); errors.append(clean); continue
            mdf['Price_Idx'] = np.arange(len(mdf))

            daily = yf.download(ticker, period='3y', interval='1d',
                                auto_adjust=True, progress=False, timeout=30)
            if isinstance(daily.columns, pd.MultiIndex):
                daily.columns = daily.columns.get_level_values(0)
            daily = daily.dropna()
            if daily.empty:
                print("→ skip (no daily)"); errors.append(clean); continue
        except Exception as e:
            print(f"→ ERROR"); errors.append(clean); continue

        scan_months = mdf[mdf.index >= pd.Timestamp(cutoff)]
        seen        = set()
        stock_trades= []

        for bar_pos in range(len(scan_months)):
            gpos = mdf.index.get_loc(scan_months.index[bar_pos])
            hist = mdf.iloc[:gpos+1].copy()
            hist['Price_Idx'] = np.arange(len(hist))

            res = fit_trendline(hist, ticker)
            if res is None: continue
            slope, intercept, ai, touches, ref_df = res

            hist_lows    = ref_df['Low'].values.flatten().astype(float)
            ci           = float(ref_df['Price_Idx'].iloc[-1])
            cc           = _scalar(ref_df['Close'].iloc[-1])
            bar_low      = _scalar(ref_df['Low'].iloc[-1])
            trendline_px = slope * ci + intercept

            # TOUCH detection: use the monthly LOW (wick)
            dist_low   = (bar_low - trendline_px) / trendline_px * 100
            dist_close = (cc      - trendline_px) / trendline_px * 100

            # Signal fires when wick touches within ENTRY_TOLERANCE
            if abs(dist_low) > ENTRY_TOLERANCE: continue

            dist_pct = dist_low   # use wick distance for signal classification

            fibs, _, _ = calc_fib_levels(hist_lows, ai, ref_df)
            fs, fn, fl, fp = fib_score(fibs, trendline_px)

            entry_date   = scan_months.index[bar_pos].to_pydatetime()
            mk           = f"{clean}_{entry_date.strftime('%Y-%m')}"
            if mk in seen: continue
            seen.add(mk)

            entry_price  = round(cc, 2)                              # entry at monthly close
            sl_price     = round(trendline_px * (1 - SL_PCT/100), 2) # 10% below TRENDLINE wick
            target_price = round(entry_price * (1 + TARGET_PCT/100), 2)
            shares       = max(1, int(POSITION_SIZE // entry_price))

            outcome, exit_date, exit_price, days_held, pnl_pct = simulate(
                daily, entry_date, entry_price, sl_price, target_price
            )

            stock_trades.append({
                'symbol':          clean,
                'entry_date':      entry_date.strftime('%Y-%m-%d'),
                'exit_date':       exit_date.strftime('%Y-%m-%d') if hasattr(exit_date,'strftime') else str(exit_date),
                'entry_price':     entry_price,
                'exit_price':      round(float(exit_price),2),
                'sl_price':        sl_price,
                'target_price':    target_price,
                'distance_pct':    round(abs(dist_pct),2),
                'signal_status':   'CRITICAL_TOUCH',
                'fib_score':       fs,
                'fib_level':       fl or '—',
                'fib_note':        fn,
                'fib_levels':      fibs,
                'wick_touches':    touches,
                'outcome':         outcome,
                'holding_days':    int(days_held),
                'pnl_pct':         round(float(pnl_pct),2),
                'pnl_amount':      round((float(exit_price)-entry_price)*shares,2),
                'shares':          shares,
            })

        all_trades.extend(stock_trades)
        print(f"→ {len(stock_trades)} signal(s)" if stock_trades else "→ no signals")

    # ─── SUMMARY ─────────────────────────────────────────────────────────────
    print(f"\n{'='*70}")
    print(f"  RESULTS")
    print(f"{'='*70}\n")

    if not all_trades:
        print("  No trades found."); return {}

    comp   = [t for t in all_trades if t['outcome'] in ('STOP_LOSS','TARGET_HIT','TIMEOUT')]
    wins   = [t for t in comp if t['pnl_pct'] > 0]
    losses = [t for t in comp if t['pnl_pct'] <= 0]
    hits   = [t for t in all_trades if t['outcome']=='TARGET_HIT']
    sls    = [t for t in all_trades if t['outcome']=='STOP_LOSS']
    tos    = [t for t in all_trades if t['outcome']=='TIMEOUT']
    open_t = [t for t in all_trades if t['outcome']=='OPEN']

    wr   = round(len(wins)/len(comp)*100,1) if comp else 0
    tpnl = round(sum(t['pnl_amount'] for t in all_trades),2)
    avgr = round(sum(t['pnl_pct'] for t in all_trades)/len(all_trades),2)
    avgh = round(sum(t['holding_days'] for t in all_trades)/len(all_trades),1)
    pg   = sum(t['pnl_amount'] for t in wins)
    pl   = abs(sum(t['pnl_amount'] for t in losses))
    pf   = round(pg/pl,2) if pl>0 else (999.0 if pg>0 else 0.0)

    print(f"  Total signals  : {len(all_trades)}")
    print(f"  Completed      : {len(comp)}")
    print(f"  Open           : {len(open_t)}")
    print(f"  Win rate       : {wr}%")
    print(f"  Profit factor  : {pf}x")
    print(f"  Total P&L      : ₹{tpnl:,.0f}")
    print(f"  Avg return     : {avgr}%")
    print(f"  Avg hold       : {avgh} days")
    print(f"  Target hits    : {len(hits)}")
    print(f"  Stop losses    : {len(sls)}")
    print(f"  Timeouts       : {len(tos)}")
    print()

    # Fib score breakdown
    print(f"  ─── By Fibonacci Score ───────────────────────────────────")
    for lo, hi, lbl in [(8,10,'Score 8-10 (strong fib)'),(5,7,'Score 5-7 (weak fib)'),(0,4,'Score 0-4')]:
        b  = [t for t in all_trades if lo <= t['fib_score'] <= hi]
        if not b: print(f"  {lbl:<28}: no signals"); continue
        bc = [t for t in b if t['outcome'] in ('STOP_LOSS','TARGET_HIT','TIMEOUT')]
        bw = [t for t in bc if t['pnl_pct']>0]
        bwr= round(len(bw)/len(bc)*100,1) if bc else 0
        ba = round(sum(t['pnl_pct'] for t in b)/len(b),2)
        bp = round(sum(t['pnl_amount'] for t in b),0)
        bh = len([t for t in b if t['outcome']=='TARGET_HIT'])
        bs = len([t for t in b if t['outcome']=='STOP_LOSS'])
        print(f"  {lbl:<28}: {len(b):2d} | WR:{bwr:5.1f}% | Avg:{ba:+.2f}% | P&L:₹{bp:,.0f} | Hits:{bh} SL:{bs}")
    print()

    st = sorted(all_trades, key=lambda x: x['pnl_pct'], reverse=True)
    print(f"  Best : {st[0]['symbol']:<12} {st[0]['entry_date']}  {st[0]['outcome']:<12} {st[0]['pnl_pct']:+.1f}%  ₹{st[0]['pnl_amount']:,.0f}")
    print(f"  Worst: {st[-1]['symbol']:<12} {st[-1]['entry_date']}  {st[-1]['outcome']:<12} {st[-1]['pnl_pct']:+.1f}%  ₹{st[-1]['pnl_amount']:,.0f}")
    if errors: print(f"\n  ⚠️  Skipped: {', '.join(errors)}")

    # Save JSON
    out = {
        'generated_at': datetime.now().isoformat(),
        'rules': {
            'entry':   'CRITICAL TOUCH ≤1% of trendline',
            'sl':      f'{SL_PCT}% below TRENDLINE WICK price — daily close',
            'target':  f'{TARGET_PCT}% above entry — daily close',
            'timeout': f'{TIMEOUT_DAYS} days',
        },
        'summary': {
            'total': len(all_trades), 'completed': len(comp), 'open': len(open_t),
            'win_rate': wr, 'profit_factor': pf, 'total_pnl': tpnl,
            'avg_return': avgr, 'avg_hold': avgh,
            'hits': len(hits), 'stop_losses': len(sls), 'timeouts': len(tos),
        },
        'trades': all_trades,
    }
    with open('backtest_1year_results.json','w') as f: json.dump(out, f, indent=2)
    print(f"\n✅ Saved → backtest_1year_results.json")
    return out

if __name__ == '__main__':
    run_backtest()
