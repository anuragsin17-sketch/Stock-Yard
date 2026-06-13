#!/usr/bin/env python3
"""
Nifty 500 — 1-Year Trendline Backtest
======================================
EXACT RULES:
  Entry  : CRITICAL TOUCH — price within 1% of ascending trendline
  SL     : 10% below TRENDLINE WICK price — triggered on any daily close
  Target : 23% above entry — triggered on any daily close
  Timeout: 365 days
  Universe: Nifty 500 (~504 stocks)
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
SL_PCT           = 10.0   # 10% below TRENDLINE WICK — daily close check
TARGET_PCT       = 23.0   # 23% above entry — daily close check
TIMEOUT_DAYS     = 365
WICK_TOLERANCE   = 8.0
MIN_WICK_TOUCHES = 3
ENTRY_TOLERANCE  = 5.0    # within 5% of trendline
BACKTEST_YEARS   = 1

CSV_PATH = '../ind_nifty500list (1).csv'

# ─── HELPERS ─────────────────────────────────────────────────────────────────

def _scalar(val):
    if hasattr(val, 'iloc'):    return float(val.iloc[0])
    if hasattr(val, '__len__') and not isinstance(val, str): return float(val.flat[0])
    return float(val)

def fit_trendline(mdf, ticker=''):
    """
    Best ascending trendline — works with any data length, even 3-4 years.
    Accuracy-weighted scoring ensures trendline fits actual anchor lows.
    """
    if len(mdf) < 24: return None
    lows = mdf['Low'].values.flatten().astype(float)
    n_bars = len(mdf)
    banking = ['SBIN','HDFCBANK','ICICIBANK','AXISBANK','KOTAKBANK','INDUSINDBK']
    order = 6 if any(b in ticker.upper() for b in banking) else 10
    all_anchors = set()
    for o in [order, 8, 6, 5, 4, 3]:
        tb = argrelextrema(lows, np.less, order=o)
        for idx in tb[0]: all_anchors.add(int(idx))
    all_anchors = sorted(all_anchors)
    if len(all_anchors) < 2: return None

    best_result, best_score = None, -1
    for i in range(len(all_anchors)-1):
        a1 = all_anchors[i]
        if a1 > int(n_bars*0.80): break
        for j in range(i+1, len(all_anchors)):
            a2 = all_anchors[j]
            if a2-a1 < 12: continue
            x=[float(mdf['Price_Idx'].iloc[a1]),float(mdf['Price_Idx'].iloc[a2])]
            y=[lows[a1],lows[a2]]
            slope,intercept=np.polyfit(x,y,1)
            if slope<=0: continue
            # CRITICAL RULE: No monthly close below trendline (2% buffer)
            closes_arr = mdf['Close'].values.flatten().astype(float)
            broken = any(
                closes_arr[k] < (slope*float(mdf['Price_Idx'].iloc[k])+intercept)*0.98
                for k in range(n_bars)
                if (slope*float(mdf['Price_Idx'].iloc[k])+intercept) > 0
            )
            if broken: continue
            touch_list=[]
            for k in range(n_bars):
                tl_p=slope*float(mdf['Price_Idx'].iloc[k])+intercept
                if tl_p<=0: continue
                if abs((lows[k]-tl_p)/tl_p)*100<=WICK_TOLERANCE:
                    touch_list.append(k)
            if len(touch_list)<MIN_WICK_TOUCHES: continue
            tl_at_a1=slope*float(mdf['Price_Idx'].iloc[a1])+intercept
            tl_at_a2=slope*float(mdf['Price_Idx'].iloc[a2])+intercept
            accuracy=1.0/(1.0+abs((lows[a1]-tl_at_a1)/lows[a1])+abs((lows[a2]-tl_at_a2)/lows[a2]))
            recency=max(touch_list)/n_bars
            score=len(touch_list)*10+(a2-a1)*0.05+recency*20+accuracy*40
            if score>best_score:
                best_score=score
                best_result=(slope,intercept,[a1,a2],len(touch_list))
    if best_result is None: return None
    return best_result

def calc_fib_levels(lows, ai, mdf):
    try:
        lp = float(lows[ai[-1]])
        highs = mdf.iloc[int(ai[-1]):]['High'].values.flatten().astype(float)
        mx = argrelextrema(highs, np.greater, order=3)[0]
        sh = float(highs[mx].max()) if len(mx) > 0 else float(highs.max())
        fr = sh - lp
        if fr <= 0: return {}, None, None
        return {
            '23.6%': round(sh-fr*0.236,2), '38.2%': round(sh-fr*0.382,2),
            '50.0%': round(sh-fr*0.500,2), '61.8%': round(sh-fr*0.618,2),
            '78.6%': round(sh-fr*0.786,2), '100.0%': round(sh-fr*1.000,2),
        }, lp, sh
    except: return {}, None, None

def fib_score(fib_levels, trigger):
    if not fib_levels: return 5, 'No fib', None, None
    md, cl, cp = float('inf'), None, None
    for name, price in fib_levels.items():
        d = abs((trigger-price)/price)*100
        if d < md: md, cl, cp = d, name, price
    if md <= 1.5:
        s = 10 if md<=0.3 else (9 if md<=0.7 else 8)
        if cl=='61.8%': s = min(10,s+1)
        note = f"Fib {cl} ({md:.1f}%) ✓"
    elif md <= 3.0: s, note = 7, f"Near {cl} ({md:.1f}%)"
    else:           s, note = 5, f"Nearest {cl} ({md:.1f}%)"
    return s, note, cl, cp

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
        lp = _scalar(sub['Close'].iloc[-1])
        d  = (sub.index[-1] - pd.Timestamp(entry_date)).days
        return 'TIMEOUT', sub.index[-1], round(lp,2), d, round((lp-entry_price)/entry_price*100,2)
    except: return 'ERROR', entry_date, entry_price, 0, 0.0

# ─── MAIN ────────────────────────────────────────────────────────────────────

def run():
    print(f"\n{'='*70}")
    print(f"  NIFTY 500 — 1-YEAR TRENDLINE BACKTEST")
    print(f"  Entry  : CRITICAL TOUCH <=1% of ascending trendline")
    print(f"  SL     : {SL_PCT}% below TRENDLINE WICK — daily close")
    print(f"  Target : {TARGET_PCT}% above entry — daily close")
    print(f"  Period : {(datetime.now()-timedelta(days=365)).strftime('%Y-%m-%d')} → {datetime.now().strftime('%Y-%m-%d')}")
    print(f"{'='*70}\n")

    # Load tickers
    df_csv   = pd.read_csv(CSV_PATH)
    tickers  = [str(s).strip() for s in df_csv['Symbol'].tolist()]
    print(f"  Loaded {len(tickers)} tickers from CSV\n")

    cutoff     = datetime.now() - timedelta(days=BACKTEST_YEARS*365)
    all_trades = []
    errors     = []
    t_start    = time.time()

    for i, sym in enumerate(tickers, 1):
        ticker = sym + '.NS'
        elapsed = time.time() - t_start
        eta_str = ''
        if i > 5:
            eta = (elapsed / i) * (len(tickers) - i)
            eta_str = f" ETA:{int(eta//60)}m{int(eta%60)}s"
        print(f"  [{i:3d}/{len(tickers)}] {sym:<14}", end='', flush=True)

        try:
            mdf = yf.download(ticker, period='10y', interval='1mo',
                              auto_adjust=True, progress=False, timeout=20)
            if isinstance(mdf.columns, pd.MultiIndex):
                mdf.columns = mdf.columns.get_level_values(0)
            mdf = mdf.dropna()
            if len(mdf) < 24:
                print(f"→ skip{eta_str}"); errors.append(sym); continue
            mdf['Price_Idx'] = np.arange(len(mdf))

            daily = yf.download(ticker, period='3y', interval='1d',
                                auto_adjust=True, progress=False, timeout=20)
            if isinstance(daily.columns, pd.MultiIndex):
                daily.columns = daily.columns.get_level_values(0)
            daily = daily.dropna()
            if daily.empty:
                print(f"→ no daily{eta_str}"); errors.append(sym); continue

        except Exception as e:
            print(f"→ ERROR{eta_str}"); errors.append(sym); continue

        scan_months = mdf[mdf.index >= pd.Timestamp(cutoff)]
        seen, stock_trades = set(), []

        for bar_pos in range(len(scan_months)):
            gpos = mdf.index.get_loc(scan_months.index[bar_pos])
            hist = mdf.iloc[:gpos+1].copy()
            hist['Price_Idx'] = np.arange(len(hist))

            res = fit_trendline(hist, ticker)
            if res is None: continue
            slope, intercept, ai, touches = res

            hist_lows    = hist['Low'].values.flatten().astype(float)
            ci           = float(hist['Price_Idx'].iloc[-1])
            cc           = _scalar(hist['Close'].iloc[-1])
            bar_low      = _scalar(hist['Low'].iloc[-1])
            trendline_px = slope * ci + intercept
            dist_close   = (cc - trendline_px) / trendline_px * 100
            dist_low     = (bar_low - trendline_px) / trendline_px * 100
            dist_pct     = dist_close if abs(dist_close) <= abs(dist_low) else dist_low

            if abs(dist_pct) > ENTRY_TOLERANCE: continue

            fibs, _, _ = calc_fib_levels(hist_lows, ai, hist)
            fs, fn, fl, fp = fib_score(fibs, trendline_px)

            entry_date = scan_months.index[bar_pos].to_pydatetime()
            mk = f"{sym}_{entry_date.strftime('%Y-%m')}"
            if mk in seen: continue
            seen.add(mk)

            entry_price  = round(cc, 2)                               # entry at monthly close
            sl_price     = round(trendline_px * (1 - SL_PCT/100), 2)  # 10% below TRENDLINE wick
            target_price = round(entry_price * (1 + TARGET_PCT/100), 2)
            shares       = max(1, int(POSITION_SIZE // entry_price))

            outcome, exit_date, exit_price, days_held, pnl_pct = simulate(
                daily, entry_date, entry_price, sl_price, target_price
            )

            stock_trades.append({
                'symbol':       sym,
                'entry_date':   entry_date.strftime('%Y-%m-%d'),
                'exit_date':    exit_date.strftime('%Y-%m-%d') if hasattr(exit_date,'strftime') else str(exit_date),
                'entry_price':  entry_price,
                'exit_price':   round(float(exit_price),2),
                'sl_price':     sl_price,
                'target_price': target_price,
                'distance_pct': round(abs(dist_pct),2),
                'fib_score':    fs,
                'fib_level':    fl or '—',
                'fib_note':     fn,
                'wick_touches': touches,
                'outcome':      outcome,
                'holding_days': int(days_held),
                'pnl_pct':      round(float(pnl_pct),2),
                'pnl_amount':   round((float(exit_price)-entry_price)*shares,2),
                'shares':       shares,
            })

        all_trades.extend(stock_trades)
        sig_str = f"→ {len(stock_trades)} signal(s){eta_str}" if stock_trades else f"→ —{eta_str}"
        print(sig_str)

        # Save partial results every 50 stocks
        if i % 50 == 0:
            with open('backtest_500_partial.json','w') as f:
                json.dump({'trades': all_trades, 'processed': i}, f)
            print(f"\n  💾 Partial save: {len(all_trades)} trades so far\n")

        time.sleep(0.1)

    # ─── FINAL SUMMARY ───────────────────────────────────────────────────────
    print(f"\n{'='*70}")
    print(f"  FINAL RESULTS — {len(tickers)} stocks, 1 Year")
    print(f"{'='*70}\n")

    if not all_trades:
        print("  No trades found."); return

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
    print(f"  Avg return/trade: {avgr}%")
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
        bpf_n = sum(t['pnl_amount'] for t in bw)
        bpf_d = abs(sum(t['pnl_amount'] for t in [x for x in bc if x['pnl_pct']<=0]))
        bpf   = round(bpf_n/bpf_d,2) if bpf_d>0 else 999.0
        print(f"  {lbl:<28}: {len(b):3d} | WR:{bwr:5.1f}% | PF:{bpf:4.2f}x | Avg:{ba:+.2f}% | P&L:₹{bp:,.0f} | Hits:{bh} SL:{bs}")

    # Top 5 winners / losers
    st = sorted(all_trades, key=lambda x: x['pnl_pct'], reverse=True)
    print(f"\n  ─── Top 5 Winners ────────────────────────────────────────")
    for t in st[:5]:
        print(f"    {t['symbol']:<14} {t['entry_date']}  {t['pnl_pct']:+.1f}%  ₹{t['pnl_amount']:,.0f}")
    print(f"  ─── Top 5 Losers  ────────────────────────────────────────")
    for t in st[-5:]:
        print(f"    {t['symbol']:<14} {t['entry_date']}  {t['pnl_pct']:+.1f}%  ₹{t['pnl_amount']:,.0f}")

    if errors:
        print(f"\n  ⚠️  {len(errors)} tickers skipped")

    elapsed_total = time.time() - t_start
    print(f"\n  ⏱️  Total time: {int(elapsed_total//60)}m {int(elapsed_total%60)}s")

    # Save final JSON
    out = {
        'generated_at': datetime.now().isoformat(),
        'rules': {
            'entry':   'CRITICAL TOUCH ≤1% of trendline',
            'sl':      f'{SL_PCT}% below TRENDLINE WICK — daily close',
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
    with open('backtest_500_results.json','w') as f: json.dump(out, f, indent=2)
    print(f"\n✅ Final results saved → backtest_500_results.json")

if __name__ == '__main__':
    run()
