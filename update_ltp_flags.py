#!/usr/bin/env python3
# encoding: utf-8
"""
Fetch current LTP for all stocks in volume_gainer_watchlist.json,
compute dist_pct and in_entry_zone flag, save and push to public repo.
"""
import json, time, warnings, shutil, sys
warnings.filterwarnings('ignore')
import yfinance as yf

WATCHLIST_FILE = 'volume_gainer_watchlist.json'
ENTRY_LOW  = -10.0
ENTRY_HIGH =   5.0   # matches frontend filter (+5% upper bound)

with open(WATCHLIST_FILE) as f:
    watchlist = json.load(f)

tickers = list(set(w['ticker'] for w in watchlist if w.get('ticker')))
print(f"Fetching LTP for {len(tickers)} unique tickers...")

ltp_map = {}
BATCH = 100
ns_tickers = [t + '.NS' for t in tickers]

for i in range(0, len(ns_tickers), BATCH):
    batch_ns  = ns_tickers[i:i+BATCH]
    batch_sym = tickers[i:i+BATCH]
    try:
        data = yf.download(batch_ns, period='2d', interval='1d',
                           auto_adjust=True, progress=False,
                           group_by='ticker', threads=True)
        for sym, ns in zip(batch_sym, batch_ns):
            try:
                df = data[ns] if len(batch_ns) > 1 else data
                df = df.dropna(subset=['Close'])
                if not df.empty:
                    ltp_map[sym] = round(float(df['Close'].iloc[-1]), 2)
            except: pass
    except Exception as e:
        print(f"  Batch error: {e}")
    print(f"  {min(i+BATCH, len(tickers))}/{len(tickers)} done")
    time.sleep(0.3)

print(f"LTPs fetched: {len(ltp_map)}/{len(tickers)}")

# Update flags
in_zone = 0
from datetime import datetime
now = datetime.now().isoformat()

for w in watchlist:
    sym   = w['ticker']
    entry = w.get('prev_day_low', 0)
    ltp   = ltp_map.get(sym, 0)
    if ltp > 0 and entry > 0:
        dist = round((ltp - entry) / entry * 100, 2)
        w['ltp']            = ltp
        w['dist_pct']       = dist
        w['in_entry_zone']  = ENTRY_LOW <= dist <= ENTRY_HIGH
        w['ltp_updated_at'] = now
        if w['in_entry_zone']:
            in_zone += 1
    else:
        w['in_entry_zone'] = False
        w['dist_pct']      = None
        w['ltp']           = None

# Sort: in_entry_zone first, then nearest dist
watchlist.sort(key=lambda x: (
    0 if x.get('in_entry_zone') else 1,
    abs(x.get('dist_pct') or 999)
))

with open(WATCHLIST_FILE, 'w') as f:
    json.dump(watchlist, f, indent=2)

# Copy to public repo
shutil.copy(WATCHLIST_FILE, '../volume_gainer_watchlist.json')

print(f"\nTotal: {len(watchlist)}  |  In entry zone (-10% to +4%): {in_zone}")
print("\nStocks in entry zone:")
print(f"  {'Ticker':<14} {'Added':^12} {'Entry':>8} {'LTP':>8} {'Dist%':>7}")
print(f"  {'-'*55}")
for w in watchlist:
    if w.get('in_entry_zone'):
        print(f"  {w['ticker']:<14} {w.get('added_date',''):^12} "
              f"Rs{w['prev_day_low']:>7,.1f} Rs{w['ltp']:>7,.1f} {w['dist_pct']:>+6.1f}%")

print(f"\nDone. Now push volume_gainer_watchlist.json in Stock-Yard-Public.")
