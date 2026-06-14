#!/usr/bin/env python3
"""Debug ADANIPORTS trades from backtest"""
import warnings, pandas as pd, numpy as np
from scipy.signal import argrelextrema
from datetime import datetime, timedelta
import yfinance as yf
warnings.filterwarnings('ignore')

# Import the actual fit_trendline from backtest
import sys
sys.path.insert(0, '.')
from backtest_1year_nifty50 import fit_trendline, _scalar, WICK_TOLERANCE, ENTRY_TOLERANCE

ticker = 'ADANIPORTS.NS'
print(f"\nDebugging ADANIPORTS — full monthly scan")
print("="*65)

mdf = yf.download(ticker, period='10y', interval='1mo', auto_adjust=True, progress=False)
if isinstance(mdf.columns, pd.MultiIndex): mdf.columns = mdf.columns.get_level_values(0)
mdf = mdf.dropna()
mdf['Price_Idx'] = np.arange(len(mdf))
print(f"Data: {len(mdf)} months ({mdf.index[0].strftime('%Y-%m')} to {mdf.index[-1].strftime('%Y-%m')})\n")

cutoff = datetime.now() - timedelta(days=365)
scan_months = mdf[mdf.index >= pd.Timestamp(cutoff)]
print(f"{'Month':<10} {'Close':>8} {'Low':>8} {'TL':>8} {'Dist%':>7}  Anchors")
print("-"*65)

for bar_pos in range(len(scan_months)):
    gpos = mdf.index.get_loc(scan_months.index[bar_pos])
    hist = mdf.iloc[:gpos+1].copy()
    hist['Price_Idx'] = np.arange(len(hist))

    res = fit_trendline(hist, ticker)
    if res is None:
        print(f"{scan_months.index[bar_pos].strftime('%Y-%m'):<10} {'no trendline':>30}")
        continue

    slope, intercept, ai, touches = res
    hist_lows = hist['Low'].values.flatten().astype(float)
    ci   = float(hist['Price_Idx'].iloc[-1])
    cc   = _scalar(hist['Close'].iloc[-1])
    bl   = _scalar(hist['Low'].iloc[-1])
    tl   = slope * ci + intercept
    dc   = (cc - tl) / tl * 100
    dl   = (bl - tl) / tl * 100
    dist = dc if abs(dc) <= abs(dl) else dl

    a1, a2 = ai[0], ai[-1]
    anchor_str = f"{hist.index[a1].strftime('%Y-%m')}(₹{hist_lows[a1]:.0f})→{hist.index[a2].strftime('%Y-%m')}(₹{hist_lows[a2]:.0f})"
    signal = " <<< SIGNAL" if abs(dist) <= ENTRY_TOLERANCE else ""
    print(f"{scan_months.index[bar_pos].strftime('%Y-%m'):<10} {cc:>8.0f} {bl:>8.0f} {tl:>8.0f} {dist:>+7.1f}%  {anchor_str}{signal}")
