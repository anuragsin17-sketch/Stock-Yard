import pandas as pd
import numpy as np
from scipy.signal import argrelextrema
from datetime import datetime, timedelta
import yfinance as yf
import warnings
warnings.filterwarnings('ignore')

ticker = 'DCMSHRIRAM.NS'
mdf = yf.download(ticker, period='8y', interval='1mo', auto_adjust=True, progress=False)
mdf = mdf.dropna()
mdf['Price_Idx'] = np.arange(len(mdf))
daily = yf.download(ticker, period='6y', interval='1d', auto_adjust=True, progress=False).dropna()

print(f"Monthly rows: {len(mdf)}, Daily rows: {len(daily)}")

cutoff = datetime.now() - timedelta(days=2*365)
scan_months = mdf[mdf.index >= pd.Timestamp(cutoff)]
print(f"Scan months (2Y window): {len(scan_months)}")

WICK_TOL = 5.0
MIN_TOUCHES = 3
CONFLUENCE_MIN = 7
SL_PCT = 8.0
TARGET_PCT = 20.0

passed = 0
for bar_pos in range(len(scan_months)):
    gpos = mdf.index.get_loc(scan_months.index[bar_pos])
    hist = mdf.iloc[:gpos + 1].copy()
    hist['Price_Idx'] = np.arange(len(hist))
    lows = hist['Low'].values.flatten()
    bar_date = scan_months.index[bar_pos].strftime('%Y-%m')

    # Anchors
    tb = argrelextrema(lows, np.less, order=10)
    for o in [8, 6, 5]:
        if len(tb[0]) >= 2: break
        tb = argrelextrema(lows, np.less, order=o)
    if len(tb[0]) < 2:
        print(f"  {bar_date}: SKIP anchors={len(tb[0])}")
        continue

    n = min(3, len(tb[0]))
    ai = tb[0][-n:]
    x = [hist['Price_Idx'].iloc[i] for i in ai]
    y = [lows[i] for i in ai]
    slope, intercept = np.polyfit(x, y, 1)
    if slope <= 0:
        print(f"  {bar_date}: SKIP slope={slope:.4f}")
        continue

    wicks = sum(1 for i in range(len(hist))
                if abs((lows[i] - (slope * hist['Price_Idx'].iloc[i] + intercept))
                       / (slope * hist['Price_Idx'].iloc[i] + intercept)) * 100 <= WICK_TOL)
    if wicks < MIN_TOUCHES:
        print(f"  {bar_date}: SKIP wicks={wicks}")
        continue

    ci = hist['Price_Idx'].iloc[-1]
    cc = float(hist['Close'].iloc[-1])
    trigger = slope * ci + intercept
    dist = (cc - trigger) / trigger * 100

    # Fib score
    lp = float(lows[ai[-1]])
    after = hist.iloc[ai[-1]:]
    highs_a = after['High'].values
    mx = argrelextrema(highs_a, np.greater, order=3)[0]
    sh = float(after['High'].iloc[mx].max()) if len(mx) > 0 else float(after['High'].max())
    fr = sh - lp
    fib_score = 5
    if fr > 0:
        lvls = {k: sh - fr*v for k,v in [('38.2',0.382),('50.0',0.500),('61.8',0.618),('78.6',0.786),('100',1.0)]}
        md = min(abs((trigger-p)/p)*100 for p in lvls.values())
        cl = min(lvls, key=lambda k: abs((trigger-lvls[k])/lvls[k])*100)
        if md <= 1.5:
            fib_score = 10 if md <= 0.3 else (9 if md <= 0.7 else 8)
            if cl == '61.8': fib_score = min(10, fib_score+1)

    status = "CRITICAL" if abs(dist) <= 1.0 else "WATCHLIST"
    print(f"  {bar_date}: dist={dist:.2f}% wicks={wicks} fib={fib_score} trigger={trigger:.2f} close={cc:.2f} [{status}] {'PASS' if fib_score >= CONFLUENCE_MIN else 'SKIP fib<7'}")
    if fib_score >= CONFLUENCE_MIN:
        passed += 1

print(f"\nPassed all filters: {passed}/{len(scan_months)} bars")
