#!/usr/bin/env python3
"""
1-Year Backtest — Nifty 500 | Your Exact Rules
================================================
Entry  : CRITICAL TOUCH — price within ≤1% of ascending trendline
Target : 23% above entry — triggered on any daily CLOSE
SL     : 8% below TRENDLINE WICK price — triggered on MONTHLY CLOSE only
         (intraday wicks below 8% are completely ignored)
Timeout: 365 days
"""

import json
import warnings
import pandas as pd
import numpy as np
from scipy.signal import argrelextrema
from datetime import datetime, timedelta
import yfinance as yf
import time

warnings.filterwarnings('ignore')

# ─── CONFIG ──────────────────────────────────────────────────────────────────
POSITION_SIZE    = 50000.0
SL_PCT           = 8.0       # 8% from trendline WICK — monthly close only
TARGET_PCT       = 23.0      # 23% target — daily close
ENTRY_TOLERANCE  = 1.0       # CRITICAL TOUCH: price within 1% of trendline
WICK_TOLERANCE   = 8.0       # wick counted as touch if within 8% of trendline
MIN_TOUCHES      = 3         # minimum wick touches for valid trendline
TIMEOUT_DAYS     = 365       # 1 year max hold
BACKTEST_YEARS   = 1         # scan last 1 year for signals


def load_nifty500():
    """Load Nifty 500 tickers from CSV."""
    import os
    for path in ['../ind_nifty500list (1).csv', 'ind_nifty500list (1).csv',
                 '../Stock List.csv', 'Stock List.csv']:
        if os.path.exists(path):
            df = pd.read_csv(path)
            tickers = [str(s).strip() + '.NS' for s in df['Symbol'] if str(s).strip()]
            print(f"  Loaded {len(tickers)} tickers from {path}")
            return tickers
    # Fallback: hardcoded Nifty 100 if CSV not found
    print("  ⚠️  CSV not found — using hardcoded Nifty 100")
    stocks = [
        'RELIANCE','TCS','HDFCBANK','ICICIBANK','INFY','HINDUNILVR','ITC',
        'SBIN','BHARTIARTL','AXISBANK','KOTAKBANK','LT','HCLTECH','WIPRO',
        'ASIANPAINT','MARUTI','TITAN','SUNPHARMA','ULTRACEMCO','BAJFINANCE',
        'NESTLEIND','POWERGRID','NTPC','ONGC','COALINDIA','BPCL','GRASIM',
        'JSWSTEEL','TATAMOTORS','TATASTEEL','ADANIENT','ADANIPORTS','TECHM',
        'BAJAJFINSV','CIPLA','DRREDDY','DIVISLAB','EICHERMOT','HEROMOTOCO',
        'APOLLOHOSP','BRITANNIA','INDUSINDBK','HINDALCO','HDFCLIFE','SBILIFE',
        'TATACONSUM','M&M','BAJAJ-AUTO','SHRIRAMFIN','BAJAJFINSV','MPHASIS',
        'LTTS','PERSISTENT','COFORGE','LTIM','HCLTECH','OFSS',
        'PAGEIND','DMART','PIDILITIND','BERGEPAINT','KANSAINER',
        'GODREJCP','MARICO','DABUR','COLPAL','EMAMILTD',
        'VOLTAS','HAVELLS','CROMPTON','BLUESTARCO','WHIRLPOOL',
        'TATAPOWER','ADANIGREEN','CESC','TATAELXSI','ABB','SIEMENS',
        'AMBUJACEM','SHREECEM','ACC','JKLAKSHMI','RAMCOCEM',
        'IDFCFIRSTB','BANDHANBNK','FEDERALBNK','RBLBANK',
        'ICICIGI','HDFCAMC','ICICIPRU','SBICARD',
        'DLF','GODREJPROP','OBEROIRLTY','PRESTIGE','LODHA',
        'ZOMATO','NYKAA','PAYTM','POLICYBZR',
        'ASTRAL','SUPREMEIND','FINOLEX',
        'UPL','COROMANDEL','CHAMBALFERT','RCF',
        'CONCOR','IRCTC','NMDC','SAIL','HINDZINC',
        'MUTHOOTFIN','CHOLAFIN','BAJAJHLDNG','PEL',
        'UCOBANK','PNB','CANBK','BANKBARODA','INDIANB',
        'JUBLFOOD','WESTLIFE','DEVYANI','SAPPHIRE',
        'TORNTPHARM','BIOCON','ALKEM','LALPATHLAB','METROPOLIS'
    ]
    return [s + '.NS' for s in stocks]


def _scalar(val):
    if hasattr(val, 'iloc'):   return float(val.iloc[0])
    if hasattr(val, '__len__') and not isinstance(val, str): return float(val.flat[0])
    return float(val)


def fit_trendline(mdf, ticker=''):
    if len(mdf) < 24:
        return None
    lows = mdf['Low'].values.flatten().astype(float)

    banking = ['SBIN','HDFCBANK','ICICIBANK','AXISBANK','KOTAKBANK','INDUSINDBK','BANDHANBNK','IDFCFIRSTB']
    order   = 6 if any(b in ticker.upper() for b in banking) else 10

    tb = argrelextrema(lows, np.less, order=order)
    for o in [8, 6, 5, 4, 3]:
        if len(tb[0]) >= 2: break
        tb = argrelextrema(lows, np.less, order=o)

    if len(tb[0]) < 2:
        return None

    n  = min(3, len(tb[0]))
    ai = tb[0][-n:]
    x  = [float(mdf['Price_Idx'].iloc[i]) for i in ai]
    y  = [lows[i] for i in ai]
    slope, intercept = np.polyfit(x, y, 1)

    if slope <= 0:
        return None

    touches = sum(
        1 for i in range(len(mdf))
        if abs((lows[i] - (slope * float(mdf['Price_Idx'].iloc[i]) + intercept))
               / (slope * float(mdf['Price_Idx'].iloc[i]) + intercept)) * 100 <= WICK_TOLERANCE
    )
    if touches < MIN_TOUCHES:
        return None

    return slope, intercept, ai, touches


def calc_fib(lows, ai, mdf):
    try:
        lp = float(lows[ai[-1]])
        after = mdf.iloc[int(ai[-1]):]
        highs = after['High'].values.flatten().astype(float)
        mx    = argrelextrema(highs, np.greater, order=3)[0]
        sh    = float(highs[mx].max()) if len(mx) > 0 else float(highs.max())
        r     = sh - lp
        if r <= 0: return {}, None
        return {
            '23.6%':  round(sh - r*0.236, 2),
            '38.2%':  round(sh - r*0.382, 2),
            '50.0%':  round(sh - r*0.500, 2),
            '61.8%':  round(sh - r*0.618, 2),
            '78.6%':  round(sh - r*0.786, 2),
            '100.0%': round(sh - r*1.000, 2),
        }, sh
    except:
        return {}, None


def fib_score(fib_levels, trigger):
    if not fib_levels: return 5, '—', None
    md, cl, cp = float('inf'), None, None
    for k, v in fib_levels.items():
        d = abs((trigger - v) / v) * 100
        if d < md: md, cl, cp = d, k, v
    if md <= 1.5:
        s = 10 if md <= 0.3 else (9 if md <= 0.7 else 8)
        if cl == '61.8%': s = min(10, s+1)
    elif md <= 3.0:
        s = 7
    else:
        s = 5
    return s, f"{cl} ({md:.1f}%)", cp


def simulate(daily_df, mdf, entry_date, entry_price, sl_price, target_price):
    """
    Target: daily CLOSE >= target_price
    SL    : monthly CLOSE <= sl_price  (wicks ignored)
    Timeout: 365 days
    """
    try:
        fd  = daily_df[daily_df.index > pd.Timestamp(entry_date)]
        fm  = mdf[mdf.index > pd.Timestamp(entry_date)]
        ddl = pd.Timestamp(entry_date) + timedelta(days=TIMEOUT_DAYS)

        for m_dt, m_row in fm.iterrows():
            if m_dt > ddl:
                break

            # daily closes within this month
            ms = m_dt.replace(day=1)
            md = fd[(fd.index >= ms) & (fd.index <= m_dt)]

            for d_dt, d_row in md.iterrows():
                if _scalar(d_row['Close']) >= target_price:
                    held = (d_dt - pd.Timestamp(entry_date)).days
                    return 'TARGET_HIT', d_dt, target_price, held, TARGET_PCT

            # monthly close SL check
            mc = _scalar(m_row['Close'])
            if mc <= sl_price:
                held    = (m_dt - pd.Timestamp(entry_date)).days
                pnl_pct = (mc - entry_price) / entry_price * 100
                return 'STOP_LOSS', m_dt, round(mc, 2), held, round(pnl_pct, 2)

        # timeout
        sub = fd[fd.index <= ddl]
        if sub.empty:
            return 'TIMEOUT', ddl, entry_price, 0, 0.0
        lp   = _scalar(sub['Close'].iloc[-1])
        held = (sub.index[-1] - pd.Timestamp(entry_date)).days
        pnl  = (lp - entry_price) / entry_price * 100
        return 'TIMEOUT', sub.index[-1], round(lp,2), held, round(pnl,2)

    except Exception as e:
        return 'ERROR', entry_date, entry_price, 0, 0.0


# ─── MAIN ────────────────────────────────────────────────────────────────────

def run():
    tickers = load_nifty500()
    cutoff  = datetime.now() - timedelta(days=BACKTEST_YEARS * 365)

    print(f"\n{'='*72}")
    print(f"  NIFTY 500 — 1-YEAR TRENDLINE BACKTEST")
    print(f"  Entry : ≤1% of trendline (CRITICAL TOUCH)")
    print(f"  Target: {TARGET_PCT}% — daily close")
    print(f"  SL    : {SL_PCT}% below trendline WICK — monthly close only")
    print(f"  Period: {cutoff.strftime('%Y-%m-%d')} → {datetime.now().strftime('%Y-%m-%d')}")
    print(f"  Universe: {len(tickers)} stocks")
    print(f"{'='*72}\n")

    all_trades  = []
    errors      = []
    total       = len(tickers)

    for i, ticker in enumerate(tickers, 1):
        clean = ticker.replace('.NS','')
        print(f"  [{i:3d}/{total}] {clean:<14}", end='', flush=True)

        try:
            mdf = yf.download(ticker, period='8y', interval='1mo',
                              auto_adjust=True, progress=False, timeout=30)
            if isinstance(mdf.columns, pd.MultiIndex):
                mdf.columns = mdf.columns.get_level_values(0)
            mdf = mdf.dropna()
            if len(mdf) < 24:
                print(f"→ skip (only {len(mdf)} months)")
                errors.append(clean)
                continue

            mdf['Price_Idx'] = np.arange(len(mdf))

            daily = yf.download(ticker, period='3y', interval='1d',
                                auto_adjust=True, progress=False, timeout=30)
            if isinstance(daily.columns, pd.MultiIndex):
                daily.columns = daily.columns.get_level_values(0)
            daily = daily.dropna()
            if daily.empty:
                print(f"→ skip (no daily)")
                errors.append(clean)
                continue

        except Exception as e:
            print(f"→ ERROR")
            errors.append(clean)
            continue

        scan_months = mdf[mdf.index >= pd.Timestamp(cutoff)]
        seen        = set()
        stock_trades= []

        for pos in range(len(scan_months)):
            gpos = mdf.index.get_loc(scan_months.index[pos])
            hist = mdf.iloc[:gpos+1].copy()
            hist['Price_Idx'] = np.arange(len(hist))

            res = fit_trendline(hist, ticker)
            if res is None: continue

            slope, intercept, ai, touches = res
            lows = hist['Low'].values.flatten().astype(float)

            ci      = float(hist['Price_Idx'].iloc[-1])
            cc      = _scalar(hist['Close'].iloc[-1])
            tl      = slope * ci + intercept
            dist    = (cc - tl) / tl * 100

            # CRITICAL TOUCH only
            if abs(dist) > ENTRY_TOLERANCE:
                continue

            entry_date = scan_months.index[pos].to_pydatetime()
            mk = f"{clean}_{entry_date.strftime('%Y-%m')}"
            if mk in seen: continue
            seen.add(mk)

            # Fib levels
            fibs, swing_hi = calc_fib(lows, ai, hist)
            fs, fn, fp     = fib_score(fibs, tl)

            # Trade prices
            entry_price  = round(tl, 2)
            sl_price     = round(tl * (1 - SL_PCT/100), 2)   # 8% below TRENDLINE WICK
            target_price = round(entry_price * (1 + TARGET_PCT/100), 2)
            shares       = max(1, int(POSITION_SIZE // entry_price))

            outcome, exit_dt, exit_px, held, pnl_pct = simulate(
                daily, mdf, entry_date, entry_price, sl_price, target_price
            )

            stock_trades.append({
                'symbol':       clean,
                'entry_date':   entry_date.strftime('%Y-%m-%d'),
                'exit_date':    exit_dt.strftime('%Y-%m-%d') if hasattr(exit_dt,'strftime') else str(exit_dt),
                'entry_price':  entry_price,
                'sl_price':     sl_price,
                'target_price': target_price,
                'exit_price':   round(float(exit_px), 2),
                'distance_pct': round(abs(dist), 2),
                'fib_score':    fs,
                'fib_note':     fn,
                'fib_price':    round(fp, 2) if fp else None,
                'fib_levels':   fibs,
                'wick_touches': touches,
                'outcome':      outcome,
                'holding_days': int(held),
                'pnl_pct':      round(float(pnl_pct), 2),
                'pnl_amount':   round((float(exit_px) - entry_price) * shares, 2),
                'shares':       shares,
            })

        all_trades.extend(stock_trades)
        print(f"→ {len(stock_trades)} signal(s)" if stock_trades else "→ no signals")

        time.sleep(0.1)  # rate limit

    # ─── SUMMARY ─────────────────────────────────────────────────────────────
    print(f"\n{'='*72}")
    print(f"  NIFTY 500 — RESULTS")
    print(f"{'='*72}\n")

    if not all_trades:
        print("  No trades found.")
        return

    comp  = [t for t in all_trades if t['outcome'] in ('STOP_LOSS','TARGET_HIT','TIMEOUT')]
    wins  = [t for t in comp if t['pnl_pct'] > 0]
    loss  = [t for t in comp if t['pnl_pct'] <= 0]
    opn   = [t for t in all_trades if t['outcome'] == 'OPEN']

    wr    = round(len(wins)/len(comp)*100, 1) if comp else 0
    pf_n  = sum(t['pnl_amount'] for t in wins)
    pf_d  = abs(sum(t['pnl_amount'] for t in loss))
    pf    = round(pf_n/pf_d, 2) if pf_d > 0 else 999.0
    tot   = round(sum(t['pnl_amount'] for t in all_trades), 0)
    avg_r = round(sum(t['pnl_pct'] for t in all_trades)/len(all_trades), 2)
    avg_h = round(sum(t['holding_days'] for t in all_trades)/len(all_trades), 1)

    hits  = [t for t in all_trades if t['outcome']=='TARGET_HIT']
    sls   = [t for t in all_trades if t['outcome']=='STOP_LOSS']
    touts = [t for t in all_trades if t['outcome']=='TIMEOUT']

    print(f"  Total signals  : {len(all_trades)}")
    print(f"  Completed      : {len(comp)}")
    print(f"  Open           : {len(opn)}")
    print(f"  Win rate       : {wr}%")
    print(f"  Profit factor  : {pf}x")
    print(f"  Total P&L      : ₹{tot:,.0f}")
    print(f"  Avg return     : {avg_r}%")
    print(f"  Avg hold       : {avg_h} days")
    print(f"  Target hits    : {len(hits)}")
    print(f"  Stop losses    : {len(sls)}")
    print(f"  Timeouts       : {len(touts)}")

    # Fib score breakdown
    print(f"\n  ─── By Fibonacci Score ───────────────────────────────────")
    for lo, hi, lbl in [(8,10,'Score 8–10 (strong)'),(5,7,'Score 5–7 (medium)'),(0,4,'Score 0–4 (weak)')]:
        b  = [t for t in all_trades if lo <= t['fib_score'] <= hi]
        if not b: continue
        bc = [t for t in b if t['outcome'] in ('STOP_LOSS','TARGET_HIT','TIMEOUT')]
        bw = [t for t in bc if t['pnl_pct'] > 0]
        bwr= round(len(bw)/len(bc)*100,1) if bc else 0
        ba = round(sum(t['pnl_pct'] for t in b)/len(b),2)
        bp = round(sum(t['pnl_amount'] for t in b),0)
        bh = len([t for t in b if t['outcome']=='TARGET_HIT'])
        bs = len([t for t in b if t['outcome']=='STOP_LOSS'])
        print(f"  {lbl:<28}: {len(b):3d} signals | WR:{bwr:5.1f}% | Avg:{ba:+.1f}% | P&L:₹{bp:,.0f} | Hits:{bh} SL:{bs}")

    # Top / worst
    st = sorted(all_trades, key=lambda x: x['pnl_pct'], reverse=True)
    print(f"\n  Best : {st[0]['symbol']:<12} {st[0]['entry_date']}  {st[0]['outcome']:<12} {st[0]['pnl_pct']:+.1f}%  ₹{st[0]['pnl_amount']:,.0f}")
    print(f"  Worst: {st[-1]['symbol']:<12} {st[-1]['entry_date']}  {st[-1]['outcome']:<12} {st[-1]['pnl_pct']:+.1f}%  ₹{st[-1]['pnl_amount']:,.0f}")
    if errors:
        print(f"\n  ⚠️  {len(errors)} tickers skipped")

    # ─── SAVE ────────────────────────────────────────────────────────────────
    out = {
        'backtest_type': 'Nifty500_1Year_CriticalTouch',
        'generated_at':  datetime.now().isoformat(),
        'rules': {
            'entry':   'CRITICAL TOUCH ≤1% of trendline',
            'target':  f'{TARGET_PCT}% — daily close',
            'sl':      f'{SL_PCT}% below trendline wick — monthly close only',
            'timeout': f'{TIMEOUT_DAYS} days',
        },
        'parameters': {
            'position_size':  POSITION_SIZE,
            'sl_pct':         SL_PCT,
            'target_pct':     TARGET_PCT,
            'entry_tolerance':ENTRY_TOLERANCE,
            'timeout_days':   TIMEOUT_DAYS,
            'backtest_years': BACKTEST_YEARS,
        },
        'summary': {
            'total_signals': len(all_trades),
            'completed':     len(comp),
            'open_trades':   len(opn),
            'win_rate':      wr,
            'profit_factor': pf,
            'total_pnl':     float(tot),
            'avg_return':    avg_r,
            'avg_hold_days': avg_h,
            'target_hits':   len(hits),
            'stop_losses':   len(sls),
            'timeouts':      len(touts),
        },
        'trades': all_trades,
    }

    with open('backtest_nifty500_results.json','w') as f:
        json.dump(out, f, indent=2)
    print(f"\n✅ Saved → backtest_nifty500_results.json")

    # HTML
    generate_html(out, 'backtest_nifty500_report.html')
    print(f"✅ HTML  → backtest_nifty500_report.html\n")


def generate_html(data, path):
    s  = data['summary']
    tr = sorted(data['trades'], key=lambda x: x['entry_date'], reverse=True)
    r  = data['rules']

    def clr(v): return '#22c55e' if float(v)>=0 else '#ef4444'
    def oc(o):  return {'TARGET_HIT':'#22c55e','STOP_LOSS':'#ef4444','TIMEOUT':'#eab308','OPEN':'#60a5fa'}.get(o,'#94a3b8')

    rows = ''
    for t in tr:
        rows += f"""<tr>
            <td style="font-weight:700">{t['symbol']}</td>
            <td>{t['entry_date']}</td><td>{t['exit_date']}</td>
            <td style="color:#60a5fa">₹{t['entry_price']:,.2f}</td>
            <td style="color:#ef4444">₹{t['sl_price']:,.2f}</td>
            <td style="color:#22c55e">₹{t['target_price']:,.2f}</td>
            <td style="color:{clr(t['exit_price']-t['entry_price'])}">₹{t['exit_price']:,.2f}</td>
            <td style="color:#a78bfa">{t['fib_score']}/10</td>
            <td style="font-size:10px;color:#94a3b8">{t['fib_note']}</td>
            <td>{t['wick_touches']}</td>
            <td style="color:{oc(t['outcome'])};font-weight:700">{t['outcome'].replace('_',' ')}</td>
            <td>{t['holding_days']}d</td>
            <td style="color:{clr(t['pnl_pct'])};font-weight:700">{'+' if t['pnl_pct']>0 else ''}{t['pnl_pct']}%</td>
            <td style="color:{clr(t['pnl_amount'])}">₹{t['pnl_amount']:,.0f}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Nifty 500 — Trendline Backtest</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#080c14;color:#e2e8f0;font-family:'Segoe UI',sans-serif;padding:20px;font-size:12px}}
h1{{text-align:center;color:#10b981;font-size:22px;margin-bottom:4px}}
.sub{{text-align:center;color:#64748b;font-size:11px;margin-bottom:20px}}
.pills{{display:flex;gap:8px;justify-content:center;flex-wrap:wrap;margin-bottom:20px}}
.pill{{background:#111625;border:1px solid #1f2937;padding:4px 12px;border-radius:20px;font-size:11px;color:#94a3b8}}
.pill b{{color:#10b981}}
.kpi{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px;margin-bottom:20px}}
.k{{background:#111625;border:1px solid #1f2937;border-radius:8px;padding:14px;text-align:center}}
.kv{{font-size:22px;font-weight:700;margin-bottom:3px}}
.kl{{font-size:9px;color:#6b7280;text-transform:uppercase;letter-spacing:.05em}}
.box{{background:#111625;border:1px solid #1f2937;border-radius:8px;padding:16px;margin-bottom:16px}}
.box h2{{font-size:13px;font-weight:700;margin-bottom:12px;color:#f1f5f9}}
table{{width:100%;border-collapse:collapse}}
th{{background:#0d1520;color:#64748b;padding:7px 9px;text-align:left;font-weight:600;border-bottom:1px solid #1f2937;white-space:nowrap}}
td{{padding:6px 9px;border-bottom:1px solid #111825;white-space:nowrap}}
tr:hover td{{background:#131d2e}}
input{{background:#0d1520;border:1px solid #1f2937;color:#e2e8f0;padding:5px 10px;border-radius:5px;font-size:11px;width:220px;margin-bottom:10px;outline:none}}
</style></head><body>
<h1>📊 Nifty 500 — 1-Year Trendline Backtest</h1>
<p class="sub">Universe: Nifty 500 | {data['parameters']['backtest_years']} Year | Generated {datetime.now().strftime('%d %b %Y %H:%M')}</p>
<div class="pills">
  <div class="pill">Entry <b>{r['entry']}</b></div>
  <div class="pill">Target <b>{r['target']}</b></div>
  <div class="pill">SL <b>{r['sl']}</b></div>
  <div class="pill">Position <b>₹{int(data['parameters']['position_size']):,}</b></div>
</div>
<div class="kpi">
  <div class="k"><div class="kv" style="color:#10b981">{s['total_signals']}</div><div class="kl">Signals</div></div>
  <div class="k"><div class="kv" style="color:{'#22c55e' if s['win_rate']>=60 else '#eab308' if s['win_rate']>=45 else '#ef4444'}">{s['win_rate']}%</div><div class="kl">Win Rate</div></div>
  <div class="k"><div class="kv" style="color:{'#22c55e' if s['profit_factor']>=2 else '#eab308' if s['profit_factor']>=1 else '#ef4444'}">{s['profit_factor']}x</div><div class="kl">Profit Factor</div></div>
  <div class="k"><div class="kv" style="color:{'#22c55e' if s['total_pnl']>=0 else '#ef4444'}">₹{s['total_pnl']:,.0f}</div><div class="kl">Total P&L</div></div>
  <div class="k"><div class="kv" style="color:{'#22c55e' if s['avg_return']>=0 else '#ef4444'}">{s['avg_return']}%</div><div class="kl">Avg Return</div></div>
  <div class="k"><div class="kv" style="color:#60a5fa">{s['avg_hold_days']}d</div><div class="kl">Avg Hold</div></div>
  <div class="k"><div class="kv" style="color:#22c55e">{s['target_hits']}</div><div class="kl">🎯 Hits</div></div>
  <div class="k"><div class="kv" style="color:#ef4444">{s['stop_losses']}</div><div class="kl">🛑 SL</div></div>
  <div class="k"><div class="kv" style="color:#eab308">{s['timeouts']}</div><div class="kl">⏰ Timeout</div></div>
  <div class="k"><div class="kv" style="color:#60a5fa">{s['open_trades']}</div><div class="kl">📂 Open</div></div>
</div>
<div class="box">
  <h2>All Trades ({len(tr)})</h2>
  <input type="text" id="q" onkeyup="f()" placeholder="Filter by symbol, outcome...">
  <div style="overflow-x:auto;max-height:600px;overflow-y:auto">
  <table><thead><tr>
    <th>Symbol</th><th>Entry</th><th>Exit</th>
    <th>Entry ₹</th><th>SL ₹</th><th>Target ₹</th><th>Exit ₹</th>
    <th>Fib Score</th><th>Fib Level</th><th>Wicks</th>
    <th>Outcome</th><th>Hold</th><th>P&L%</th><th>P&L ₹</th>
  </tr></thead>
  <tbody id="tb">{rows}</tbody></table></div>
</div>
<script>
function f(){{
  const q=document.getElementById('q').value.toLowerCase();
  document.querySelectorAll('#tb tr').forEach(r=>{{
    r.style.display=r.textContent.toLowerCase().includes(q)?'':'none';
  }});
}}
</script>
</body></html>"""

    with open(path,'w',encoding='utf-8') as f:
        f.write(html)


if __name__ == '__main__':
    run()
