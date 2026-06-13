#!/usr/bin/env python3
"""
Trendline Visualization Tool
Generates an HTML candlestick chart showing:
  - Monthly OHLC candles
  - Algorithm's detected trendline
  - Entry signals (within 5% of trendline)
  - Anchor points used

Usage:
  python visualize_trendline.py ADANIPORTS
  python visualize_trendline.py CARTRADE
  python visualize_trendline.py COALINDIA
"""

import sys
import json
import warnings
import pandas as pd
import numpy as np
from scipy.signal import argrelextrema
from datetime import datetime, timedelta
import yfinance as yf

warnings.filterwarnings('ignore')

sys.path.insert(0, '.')
from backtest_1year_nifty50 import fit_trendline, _scalar, WICK_TOLERANCE, ENTRY_TOLERANCE

def visualize(symbol: str):
    ticker = symbol.upper().replace('.NS', '') + '.NS'
    clean  = ticker.replace('.NS', '')

    print(f"Fetching data for {clean}...")
    mdf = yf.download(ticker, period='10y', interval='1mo',
                      auto_adjust=True, progress=False)
    if isinstance(mdf.columns, pd.MultiIndex):
        mdf.columns = mdf.columns.get_level_values(0)
    mdf = mdf.dropna()
    mdf['Price_Idx'] = np.arange(len(mdf))

    if len(mdf) < 12:
        print(f"Not enough data for {clean}")
        return

    print(f"  {len(mdf)} months of data")

    # --- Detect trendline on FULL history ---
    res = fit_trendline(mdf, ticker)
    if res is None:
        print(f"  No valid trendline found for {clean}")
        tl_slope, tl_intercept, tl_anchors, tl_touches, tl_ref = None, None, [], 0, mdf
    else:
        tl_slope, tl_intercept, tl_anchors, tl_touches, tl_ref = res
        print(f"  Trendline: slope={tl_slope:.4f}, {tl_touches} wick touches")
        print(f"  Anchors: {[tl_ref.index[a].strftime('%Y-%m') for a in tl_anchors]}")

    # --- Build chart data ---
    dates      = [d.strftime('%Y-%m') for d in mdf.index]
    opens      = [round(float(v), 2) for v in mdf['Open'].values]
    highs      = [round(float(v), 2) for v in mdf['High'].values]
    lows       = [round(float(v), 2) for v in mdf['Low'].values]
    closes     = [round(float(v), 2) for v in mdf['Close'].values]
    volumes    = [int(v) for v in mdf['Volume'].values]

    # Trendline projection — only for post-2020 bars
    tl_line = [None] * len(mdf)
    if tl_slope is not None:
        for i in range(len(mdf)):
            dt = mdf.index[i]
            if dt < pd.Timestamp('2020-04-01'):
                tl_line[i] = None
                continue
            # Find this date in tl_ref
            if dt in tl_ref.index:
                idx = tl_ref.index.get_loc(dt)
                tl_line[i] = round(tl_slope * float(tl_ref['Price_Idx'].iloc[idx]) + tl_intercept, 2)

    # Anchor points
    anchor_dates  = [tl_ref.index[a].strftime('%Y-%m') for a in tl_anchors] if tl_anchors else []
    anchor_prices = [round(float(tl_ref['Low'].iloc[a]), 2) for a in tl_anchors] if tl_anchors else []

    # Signal points (within ENTRY_TOLERANCE% of trendline)
    signal_dates  = []
    signal_prices = []
    signal_labels = []
    cutoff = datetime.now() - timedelta(days=365)

    for bar_pos in range(len(mdf)):
        entry_date = mdf.index[bar_pos]
        hist = mdf.iloc[:bar_pos+1].copy()
        hist['Price_Idx'] = np.arange(len(hist))
        res2 = fit_trendline(hist, ticker)
        if res2 is None: continue
        sl2, ic2, ai2, _, ref2 = res2
        ci   = float(ref2['Price_Idx'].iloc[-1])
        cc   = _scalar(ref2['Close'].iloc[-1])
        bl   = _scalar(ref2['Low'].iloc[-1])
        tl2  = sl2 * ci + ic2
        # Use LOW (wick) for touch detection — not close
        dist_low = (bl - tl2) / tl2 * 100
        if abs(dist_low) <= ENTRY_TOLERANCE:
            signal_dates.append(entry_date.strftime('%Y-%m'))
            signal_prices.append(round(tl2, 2))
            signal_labels.append(f"W{dist_low:+.1f}%")

    print(f"  Signals (within {ENTRY_TOLERANCE}%): {len(signal_dates)}")

    # --- All wick touches ---
    wick_dates  = []
    wick_prices = []
    if tl_slope is not None:
        for i in range(len(mdf)):
            tl_p = tl_slope * float(mdf['Price_Idx'].iloc[i]) + tl_intercept
            if tl_p <= 0: continue
            dist = abs((float(mdf['Low'].iloc[i]) - tl_p) / tl_p) * 100
            if dist <= WICK_TOLERANCE:
                wick_dates.append(mdf.index[i].strftime('%Y-%m'))
                wick_prices.append(round(float(mdf['Low'].iloc[i]), 2))

    # --- Generate HTML ---
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{clean} — Trendline Chart</title>
<script src="https://cdn.jsdelivr.net/npm/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
* {{ box-sizing:border-box; margin:0; padding:0 }}
body {{ background:#080c14; color:#e2e8f0; font-family:'Segoe UI',sans-serif; padding:20px }}
h1 {{ color:#10b981; font-size:22px; margin-bottom:4px }}
.sub {{ color:#64748b; font-size:13px; margin-bottom:16px }}
.info-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; margin-bottom:20px }}
.info-card {{ background:#111625; border:1px solid #1f2937; border-radius:8px; padding:14px }}
.info-card .lbl {{ font-size:10px; color:#6b7280; text-transform:uppercase; margin-bottom:4px }}
.info-card .val {{ font-size:16px; font-weight:700; color:#f1f5f9 }}
#chart {{ width:100%; height:600px; border:1px solid #1f2937; border-radius:8px }}
.legend {{ display:flex; gap:16px; margin-top:10px; flex-wrap:wrap; font-size:12px }}
.legend-item {{ display:flex; align-items:center; gap:6px }}
.dot {{ width:12px; height:12px; border-radius:50% }}
</style>
</head>
<body>
<h1>{clean} — Trendline Detection</h1>
<p class="sub">Monthly Chart | Algorithm trendline vs price | Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>

<div class="info-grid">
  <div class="info-card">
    <div class="lbl">Current Price</div>
    <div class="val">₹{closes[-1]:,.2f}</div>
  </div>
  <div class="info-card">
    <div class="lbl">Trendline (now)</div>
    <div class="val" style="color:#3b82f6">{'₹'+str(round(tl_line[-1],2)) if tl_line[-1] else '—'}</div>
  </div>
  <div class="info-card">
    <div class="lbl">Distance to TL</div>
    <div class="val" style="color:{'#10b981' if tl_line[-1] and abs((closes[-1]-tl_line[-1])/tl_line[-1]*100)<=5 else '#f59e0b'}">
      {'—' if not tl_line[-1] else f"{((closes[-1]-tl_line[-1])/tl_line[-1]*100):+.1f}%"}
    </div>
  </div>
  <div class="info-card">
    <div class="lbl">Wick Touches</div>
    <div class="val">{tl_touches}</div>
  </div>
  <div class="info-card">
    <div class="lbl">Anchors</div>
    <div class="val" style="font-size:12px">{' → '.join(anchor_dates) if anchor_dates else '—'}</div>
  </div>
  <div class="info-card">
    <div class="lbl">Signals (1yr)</div>
    <div class="val" style="color:#10b981">{len([s for s in signal_dates if s >= (datetime.now()-timedelta(days=365)).strftime('%Y-%m')])}</div>
  </div>
</div>

<div id="chart"></div>

<div class="legend">
  <div class="legend-item"><div class="dot" style="background:#26a69a"></div> Green candle (close > open)</div>
  <div class="legend-item"><div class="dot" style="background:#ef5350"></div> Red candle (close < open)</div>
  <div class="legend-item"><div class="dot" style="background:#3b82f6"></div> Algorithm Trendline</div>
  <div class="legend-item"><div class="dot" style="background:#f59e0b"></div> Wick Touch (within 8%)</div>
  <div class="legend-item"><div class="dot" style="background:#10b981"></div> Entry Signal (within 5%)</div>
  <div class="legend-item"><div class="dot" style="background:#f43f5e"></div> Anchor Points</div>
</div>

<script>
const chart = LightweightCharts.createChart(document.getElementById('chart'), {{
    width: document.getElementById('chart').clientWidth,
    height: 600,
    layout: {{ background: {{ color: '#080c14' }}, textColor: '#94a3b8' }},
    grid: {{ vertLines: {{ color: '#1e2a38' }}, horzLines: {{ color: '#1e2a38' }} }},
    crosshair: {{ mode: LightweightCharts.CrosshairMode.Normal }},
    timeScale: {{ timeVisible: true, borderColor: '#1e2a38', fixLeftEdge: true }},
}});

// Candlestick series
const candleSeries = chart.addCandlestickSeries({{
    upColor: '#26a69a', downColor: '#ef5350',
    borderDownColor: '#ef5350', borderUpColor: '#26a69a',
    wickDownColor: '#ef5350', wickUpColor: '#26a69a',
}});

const dates = {json.dumps(dates)};
const opens = {json.dumps(opens)};
const highs = {json.dumps(highs)};
const lows  = {json.dumps(lows)};
const closes= {json.dumps(closes)};

const candleData = dates.map((d, i) => ({{
    time: d + '-01',
    open: opens[i], high: highs[i], low: lows[i], close: closes[i]
}}));
candleSeries.setData(candleData);

// Trendline
const tlLine = {json.dumps(tl_line)};
const tlData = dates.map((d, i) => tlLine[i] ? {{time: d+'-01', value: tlLine[i]}} : null).filter(Boolean);
if (tlData.length > 0) {{
    const tlSeries = chart.addLineSeries({{
        color: '#3b82f6', lineWidth: 2, lineStyle: 0,
        title: 'Trendline'
    }});
    tlSeries.setData(tlData);
}}

// Wick touches
const wickDates  = {json.dumps(wick_dates)};
const wickPrices = {json.dumps(wick_prices)};
if (wickDates.length > 0) {{
    const wickSeries = chart.addLineSeries({{
        color: 'transparent', lineWidth: 0, lastValueVisible: false, priceLineVisible: false
    }});
    const wickMarkers = wickDates.map((d, i) => ({{
        time: d + '-01', position: 'belowBar', color: '#f59e0b',
        shape: 'circle', text: 'Touch'
    }}));
    candleSeries.setMarkers(wickMarkers);
}}

// Signal markers
const sigDates  = {json.dumps(signal_dates)};
const sigLabels = {json.dumps(signal_labels)};
const anchorDates = {json.dumps(anchor_dates)};

const allMarkers = [];

// Wick touch markers
wickDates.forEach((d, i) => {{
    allMarkers.push({{ time: d+'-01', position: 'belowBar', color: '#f59e0b', shape: 'circle', text: '' }});
}});

// Signal markers
sigDates.forEach((d, i) => {{
    allMarkers.push({{ time: d+'-01', position: 'aboveBar', color: '#10b981', shape: 'arrowDown', text: 'SIGNAL '+sigLabels[i] }});
}});

// Anchor markers
anchorDates.forEach(d => {{
    allMarkers.push({{ time: d+'-01', position: 'belowBar', color: '#f43f5e', shape: 'square', text: 'ANCHOR' }});
}});

// Sort and set
allMarkers.sort((a,b) => a.time.localeCompare(b.time));
candleSeries.setMarkers(allMarkers);

// Resize
window.addEventListener('resize', () => {{
    chart.applyOptions({{ width: document.getElementById('chart').clientWidth }});
}});
</script>
</body>
</html>"""

    outfile = f'chart_{clean}.html'
    with open(outfile, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"\n✅ Chart saved → {outfile}")
    print(f"   Open in browser: file:///{outfile.replace(chr(92), '/')}")
    return outfile


if __name__ == '__main__':
    symbols = sys.argv[1:] if len(sys.argv) > 1 else ['ADANIPORTS', 'CARTRADE', 'COALINDIA']
    for sym in symbols:
        visualize(sym)
