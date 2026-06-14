#!/usr/bin/env python3
"""Debug why CARTRADE was not picked by the backtest logic"""

import warnings
import pandas as pd
import numpy as np
from scipy.signal import argrelextrema
from datetime import datetime, timedelta
import yfinance as yf

warnings.filterwarnings('ignore')

WICK_TOLERANCE   = 8.0
MIN_WICK_TOUCHES = 3
ENTRY_TOLERANCE  = 1.0
BACKTEST_YEARS   = 1

def _scalar(val):
    if hasattr(val, 'iloc'): return float(val.iloc[0])
    if hasattr(val, '__len__') and not isinstance(val, str): return float(val.flat[0])
    return float(val)

ticker = 'CARTRADE.NS'
print(f"\nDebugging: {ticker}")
print("="*60)

# Download monthly data
mdf = yf.download(ticker, period='8y', interval='1mo', auto_adjust=True, progress=False)
if isinstance(mdf.columns, pd.MultiIndex):
    mdf.columns = mdf.columns.get_level_values(0)
mdf = mdf.dropna()
print(f"Monthly bars available: {len(mdf)}")
print(f"Date range: {mdf.index[0].strftime('%Y-%m')} to {mdf.index[-1].strftime('%Y-%m')}")

if len(mdf) >= 24:
    mdf['Price_Idx'] = np.arange(len(mdf))
    lows = mdf['Low'].values.flatten().astype(float)
    n_bars = len(mdf)

    # NEW: find best trendline across all anchor combinations
    all_anchors = set()
    for o in [10, 8, 6, 5, 4, 3]:
        tb = argrelextrema(lows, np.less, order=o)
        for idx in tb[0]: all_anchors.add(int(idx))
    all_anchors = sorted(all_anchors)
    print(f"\nAll significant lows found: {len(all_anchors)}")
    for a in all_anchors:
        print(f"  Month {a} ({mdf.index[a].strftime('%Y-%m')}): Low=₹{lows[a]:.2f}")

    best_result, best_score = None, -1
    for i in range(len(all_anchors)-1):
        a1 = all_anchors[i]
        if a1 > int(n_bars*0.60): break
        for j in range(i+1, len(all_anchors)):
            a2 = all_anchors[j]
            if a2 - a1 < 24: continue
            x = [float(mdf['Price_Idx'].iloc[a1]), float(mdf['Price_Idx'].iloc[a2])]
            y = [lows[a1], lows[a2]]
            sl, ic = np.polyfit(x, y, 1)
            if sl <= 0: continue
            touch_list = []
            for k in range(n_bars):
                tl_p = sl*float(mdf['Price_Idx'].iloc[k])+ic
                if tl_p <= 0: continue
                if abs((lows[k]-tl_p)/tl_p)*100 <= 8.0:
                    touch_list.append(k)
            if len(touch_list) < 3: continue
            span  = a2 - a1
            last  = max(touch_list) / n_bars
            score = len(touch_list)*10 + span*0.1 + last*20
            if score > best_score:
                best_score  = score
                best_result = (sl, ic, a1, a2, touch_list)

    if best_result is None:
        print("\nFAIL: No valid trendline found with new logic")
    else:
        sl, ic, a1, a2, touch_list = best_result
        print(f"\nBEST TRENDLINE:")
        print(f"  Anchor 1: Month {a1} ({mdf.index[a1].strftime('%Y-%m')}) Low=₹{lows[a1]:.2f}")
        print(f"  Anchor 2: Month {a2} ({mdf.index[a2].strftime('%Y-%m')}) Low=₹{lows[a2]:.2f}")
        print(f"  Slope: {sl:.4f} | Intercept: {ic:.2f}")
        print(f"  Wick touches: {len(touch_list)}")
        print(f"\nBacktest window scan:")
        cutoff = datetime.now() - timedelta(days=365)
        scan_months = mdf[mdf.index >= pd.Timestamp(cutoff)]
        for bar_pos in range(len(scan_months)):
            gpos = mdf.index.get_loc(scan_months.index[bar_pos])
            hist = mdf.iloc[:gpos+1].copy()
            hist['Price_Idx'] = np.arange(len(hist))
            hist_lows = hist['Low'].values.flatten().astype(float)
            n2 = len(hist)
            # refit with same new logic on historical slice
            aa = set()
            for o in [10,8,6,5,4,3]:
                tb2 = argrelextrema(hist_lows, np.less, order=o)
                for idx in tb2[0]: aa.add(int(idx))
            aa = sorted(aa)
            br, bs = None, -1
            for ii in range(len(aa)-1):
                b1 = aa[ii]
                if b1 > int(n2*0.60): break
                for jj in range(ii+1, len(aa)):
                    b2 = aa[jj]
                    if b2-b1 < 24: continue
                    xh=[float(hist['Price_Idx'].iloc[b1]),float(hist['Price_Idx'].iloc[b2])]
                    yh=[hist_lows[b1],hist_lows[b2]]
                    sl2,ic2=np.polyfit(xh,yh,1)
                    if sl2<=0: continue
                    tl2=[k for k in range(n2) if abs((hist_lows[k]-(sl2*float(hist['Price_Idx'].iloc[k])+ic2))/(sl2*float(hist['Price_Idx'].iloc[k])+ic2))*100<=8.0]
                    if len(tl2)<3: continue
                    sc2=len(tl2)*10+(b2-b1)*0.1+(max(tl2)/n2)*20
                    if sc2>bs: bs,br=sc2,(sl2,ic2)
            if br is None:
                print(f"  {scan_months.index[bar_pos].strftime('%Y-%m')}: no trendline"); continue
            sl2,ic2=br
            ci=float(hist['Price_Idx'].iloc[-1])
            cc=_scalar(hist['Close'].iloc[-1])
            tl_px=sl2*ci+ic2
            dist=(cc-tl_px)/tl_px*100
            status="CRITICAL_TOUCH" if abs(dist)<=1.0 else f"dist={dist:+.2f}%"
            print(f"  {scan_months.index[bar_pos].strftime('%Y-%m')}: Price=₹{cc:.2f} | Trendline=₹{tl_px:.2f} | {status}")
