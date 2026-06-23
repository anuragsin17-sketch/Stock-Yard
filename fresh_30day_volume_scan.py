#!/usr/bin/env python3
"""
Fresh 30-Day Volume Gainer Scan
================================
Fetches NSE bhavcopy for last 30 trading days and rebuilds watchlist from scratch.

Criteria:
  - Gain >= 12%
  - Price >= ₹10
  - Entry = signal day LOW - 2% (from bhavcopy)
  - No age-based expiry (only remove when target hit)
"""

import os, json, io, zipfile, requests, time
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
from decimal import Decimal

# ── CONFIG ─────────────────────────────────────────────────────────────────────
WATCHLIST_FILE = 'volume_gainer_watchlist.json'
MIN_GAIN_PCT   = 12.0    # minimum % gain
MIN_PRICE      = 10.0    # minimum stock price ₹10
ALERT_BUFFER   = 0.05    # 5% above prev_day_low = alert zone
SL_PCT         = 0.04    # 4% SL below prev_day_low
TARGET_PCT     = 0.15    # 15% target above prev_day_low
DAYS           = 30      # scan last 30 trading days
DYNAMODB_URL   = os.environ.get('DYNAMODB_API_URL', 'https://32-194-58-75.nip.io')

# ── NSE BHAVCOPY ───────────────────────────────────────────────────────────────
def fetch_bhavcopy(date: datetime) -> pd.DataFrame:
    """Download NSE CM bhavcopy CSV for a given date."""
    headers = {
        'User-Agent': 'Mozilla/5.0',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Referer': 'https://www.nseindia.com/'
    }
    session = requests.Session()
    session.get('https://www.nseindia.com', headers=headers, timeout=15)

    date_str = date.strftime('%d%b%Y').upper()
    
    # Try new NSE URL format first
    url1 = f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{date.strftime('%Y%m%d')}_F_0000.csv.zip"
    
    # Legacy URL format
    url2 = f"https://nsearchives.nseindia.com/content/historical/EQUITIES/{date.strftime('%Y')}/{date.strftime('%b').upper()}/cm{date_str}bhav.csv.zip"
    
    for url in [url1, url2]:
        try:
            r = session.get(url, headers=headers, timeout=30)
            if r.status_code == 200 and len(r.content) > 1000:
                z = zipfile.ZipFile(io.BytesIO(r.content))
                fname = z.namelist()[0]
                df = pd.read_csv(z.open(fname))
                print(f"  ✅ {date.strftime('%Y-%m-%d')}: {len(df)} records")
                return df
        except Exception as e:
            continue
    
    print(f"  ❌ {date.strftime('%Y-%m-%d')}: failed to download")
    return None


def parse_bhavcopy(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize bhavcopy columns, keep only EQ series."""
    if df is None or df.empty:
        return pd.DataFrame()
    
    # Normalize column names
    df.columns = [c.strip() for c in df.columns]
    
    # Map both new (2026+) and old NSE format column names
    col_map = {
        'SYMBOL': ['TckrSymb', 'SYMBOL', 'TCKRSYMBOL', 'SCRIP_CD'],
        'CLOSE':  ['ClsPric', 'CLOSE', 'CLOSE_PRICE', 'CLOSEPRICE', 'CL'],
        'PREV':   ['PrvsClsgPric', 'PREVCLOSE', 'PREV_CLOSE', 'PREVIOUSCLOSE', 'PREV_CL', 'PCLOSE'],
        'OPEN':   ['OpnPric', 'OPEN', 'OPEN_PRICE', 'OP'],
        'LOW':    ['LwPric', 'LOW', 'LOW_PRICE', 'LO'],
        'HIGH':   ['HghPric', 'HIGH', 'HIGH_PRICE', 'HI'],
        'VOLUME': ['TtlTradgVol', 'TOTTRDQTY', 'VOLUME', 'TTL_TRD_QNTY', 'DELIV_QTY'],
        'SERIES': ['SctySrs', 'SERIES', 'SRS'],
    }
    
    rename = {}
    for target, candidates in col_map.items():
        for c in candidates:
            if c in df.columns:
                rename[c] = target
                break
    
    df = df.rename(columns=rename)
    
    # Keep EQ series only
    if 'SERIES' in df.columns:
        df = df[df['SERIES'].str.strip() == 'EQ'].copy()
    
    # Ensure numeric
    for col in ['CLOSE', 'OPEN', 'LOW', 'VOLUME', 'HIGH', 'PREV']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')
    
    df['SYMBOL'] = df['SYMBOL'].str.strip().str.upper()
    return df.dropna(subset=['CLOSE', 'LOW'])


def find_gainers(df: pd.DataFrame, date_str: str) -> list:
    """
    Find stocks with:
    - Gain >= MIN_GAIN_PCT
    - Close price >= MIN_PRICE
    """
    gainers = []
    
    for _, row in df.iterrows():
        close = float(row['CLOSE'])
        prev  = float(row['PREV']) if 'PREV' in row.index and pd.notna(row['PREV']) and float(row['PREV']) > 0 else 0
        low   = float(row['LOW'])
        
        if prev <= 0 or close <= 0 or low <= 0:
            continue
        
        # Skip stocks below minimum price
        if close < MIN_PRICE:
            continue
        
        gain_pct = (close - prev) / prev * 100
        
        if gain_pct >= MIN_GAIN_PCT:
            gainers.append({
                'ticker':      str(row['SYMBOL']),
                'date':        date_str,
                'close':       round(close, 2),
                'prev_close':  round(prev, 2),
                'low':         round(low, 2),  # This is the entry level
                'gain_pct':    round(gain_pct, 2),
                'volume':      int(row.get('VOLUME', 0)) if 'VOLUME' in row.index else 0,
            })
    
    return gainers


def get_prev_day_low_yf(tickers: list) -> dict:
    """Fetch previous day low for tickers using yfinance (for validation)."""
    result = {}
    ns_tickers = [t + '.NS' for t in tickers]
    BATCH = 50
    
    for i in range(0, len(ns_tickers), BATCH):
        batch = ns_tickers[i:i+BATCH]
        batch_syms = tickers[i:i+BATCH]
        try:
            data = yf.download(
                batch,
                period='5d', interval='1d',
                auto_adjust=True, progress=False,
                group_by='ticker', threads=True
            )
            for sym, ns in zip(batch_syms, batch):
                try:
                    if len(batch) == 1:
                        df = data
                    else:
                        if ns not in data.columns.get_level_values(0):
                            continue
                        df = data[ns]
                    df = df.dropna(subset=['Low'])
                    if len(df) >= 2:
                        result[sym] = round(float(df['Low'].iloc[-2]), 2)
                except Exception:
                    pass
        except Exception as e:
            print(f"    yfinance batch error: {e}")
        time.sleep(0.3)
    
    return result


def push_to_dynamodb(entries: list):
    """Push entries to DynamoDB via EC2 API."""
    if not entries:
        return
    try:
        r = requests.post(
            f"{DYNAMODB_URL}/api/save-radar",
            json={'signal_type': 'VOLUME', 'stocks': entries},
            timeout=15, verify=False
        )
        if r.status_code == 200:
            print(f"  ✅ Pushed {len(entries)} entries to DynamoDB")
        else:
            print(f"  ⚠️ DynamoDB push failed: {r.status_code}")
    except Exception as e:
        print(f"  ⚠️ DynamoDB error: {e}")


def get_trading_dates(days: int) -> list:
    """Get last N trading dates (skip weekends)."""
    dates = []
    current = datetime.now()
    
    while len(dates) < days + 10:  # fetch extra to account for holidays
        if current.weekday() < 5:  # Monday=0, Friday=4
            dates.append(current)
        current -= timedelta(days=1)
    
    return dates[:days + 5]  # return extra to handle holidays


# ── MAIN ───────────────────────────────────────────────────────────────────────
def main():
    print("=" * 80)
    print(f"FRESH 30-DAY VOLUME GAINER SCAN — {datetime.now().strftime('%Y-%m-%d %H:%M IST')}")
    print(f"Min Gain: {MIN_GAIN_PCT}%  |  Min Price: ₹{MIN_PRICE}  |  Days: {DAYS}")
    print("=" * 80)
    
    # Step 1: Empty the watchlist
    print("\n[1] Clearing watchlist...")
    empty_watchlist = {
        'last_scan_run': {
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M IST'),
            'date': datetime.now().strftime('%Y-%m-%d'),
            'new_stocks': 0,
            'total': 0
        },
        'stocks': []
    }
    with open(WATCHLIST_FILE, 'w') as f:
        json.dump(empty_watchlist, f, indent=2)
    print("  ✅ Watchlist cleared")
    
    # Step 2: Get trading dates
    print(f"\n[2] Getting last {DAYS} trading dates...")
    trading_dates = get_trading_dates(DAYS)
    print(f"  Fetching from {trading_dates[-1].strftime('%Y-%m-%d')} to {trading_dates[0].strftime('%Y-%m-%d')}")
    
    # Step 3: Fetch bhavcopy for each date
    print(f"\n[3] Fetching NSE bhavcopy for {len(trading_dates)} dates...")
    all_gainers = []
    successful_dates = 0
    
    for date in trading_dates:
        date_str = date.strftime('%Y-%m-%d')
        print(f"\n  Date: {date_str}")
        
        raw_df = fetch_bhavcopy(date)
        if raw_df is None:
            continue
        
        df = parse_bhavcopy(raw_df)
        if df.empty:
            print(f"    No EQ stocks found")
            continue
        
        print(f"    Parsed: {len(df)} EQ stocks")
        
        gainers = find_gainers(df, date_str)
        if gainers:
            print(f"    Found: {len(gainers)} stocks with ≥{MIN_GAIN_PCT}% gain and price ≥₹{MIN_PRICE}")
            all_gainers.extend(gainers)
            successful_dates += 1
        else:
            print(f"    No qualifying gainers")
    
    print(f"\n  ✅ Processed {successful_dates} dates, found {len(all_gainers)} total signals")
    
    if not all_gainers:
        print("\n❌ No gainers found in last 30 days")
        return
    
    # Step 4: Remove duplicates (keep latest signal per ticker)
    print(f"\n[4] Removing duplicates (keeping latest signal per ticker)...")
    unique_gainers = {}
    for g in sorted(all_gainers, key=lambda x: x['date'], reverse=True):
        ticker = g['ticker']
        if ticker not in unique_gainers:
            unique_gainers[ticker] = g
    
    print(f"  Unique stocks: {len(unique_gainers)}")
    
    # Step 5: Build watchlist entries
    print(f"\n[5] Building watchlist entries...")
    watchlist = []
    
    for ticker, g in unique_gainers.items():
        # Entry = signal day LOW - 2%
        entry_price = round(g['low'] * 0.98, 2)
        
        entry = {
            'ticker':          ticker,
            'company':         ticker,
            'added_date':      g['date'],
            'gain_pct':        g['gain_pct'],
            'close_price':     g['close'],
            'prev_close':      g['prev_close'],
            'prev_day_low':    entry_price,
            'alert_threshold': round(entry_price * (1 + ALERT_BUFFER), 2),
            'sl_price':        round(entry_price * (1 - SL_PCT), 2),
            'target_price':    round(entry_price * (1 + TARGET_PCT), 2),
            'vol_ratio':       0.0,
            'alerted':         False,
            'alert_sent_at':   None,
        }
        
        watchlist.append(entry)
    
    # Sort by gain_pct descending
    watchlist.sort(key=lambda x: x['gain_pct'], reverse=True)
    
    print(f"  Created {len(watchlist)} watchlist entries")
    
    # Step 6: Save watchlist
    print(f"\n[6] Saving watchlist...")
    data = {
        'last_scan_run': {
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M IST'),
            'date': datetime.now().strftime('%Y-%m-%d'),
            'new_stocks': len(watchlist),
            'total': len(watchlist)
        },
        'stocks': watchlist
    }
    
    with open(WATCHLIST_FILE, 'w') as f:
        json.dump(data, f, indent=2)
    print(f"  ✅ Saved to {WATCHLIST_FILE}")
    
    # Step 7: Push to DynamoDB
    print(f"\n[7] Pushing to DynamoDB...")
    push_to_dynamodb(watchlist)
    
    # Step 8: Summary
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"  Total signals:        {len(all_gainers)}")
    print(f"  Unique stocks:        {len(watchlist)}")
    print(f"  Date range:           {trading_dates[-1].strftime('%Y-%m-%d')} to {trading_dates[0].strftime('%Y-%m-%d')}")
    print(f"  Min gain threshold:   {MIN_GAIN_PCT}%")
    print(f"  Min price:            ₹{MIN_PRICE}")
    print(f"  Entry rule:           Signal day LOW - 2%")
    print(f"  SL:                   {SL_PCT*100}% below entry")
    print(f"  Target:               {TARGET_PCT*100}% above entry")
    print("\n  Top 10 gainers:")
    for i, e in enumerate(watchlist[:10], 1):
        print(f"    {i:2}. {e['ticker']:15} +{e['gain_pct']:5.1f}%  "
              f"Entry: ₹{e['prev_day_low']:>7,.2f}  Target: ₹{e['target_price']:>7,.2f}  ({e['added_date']})")
    
    if len(watchlist) > 10:
        print(f"    ... and {len(watchlist)-10} more")
    
    print("=" * 80)
    print(f"\n✅ Fresh 30-day scan complete!")


if __name__ == '__main__':
    main()
