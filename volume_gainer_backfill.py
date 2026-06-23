#!/usr/bin/env python3
"""
Volume Gainer Backfill - Historical Data Scanner
=================================================
Scans the last N days of NSE bhavcopy data to find all stocks that
gained ≥9% and adds them to the volume_gainer_watchlist.json.

This is a one-time backfill to catch historical opportunities that
were missed because the threshold was previously 10%.

Usage:
  python volume_gainer_backfill.py --days 60
  python volume_gainer_backfill.py --days 90 --dry-run
"""

import os, json, io, zipfile, requests, time, argparse
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta

# ── CONFIG ─────────────────────────────────────────────────────────────────────
WATCHLIST_FILE = 'volume_gainer_watchlist.json'
MIN_GAIN_PCT   = 9.0     # minimum % gain on day
ALERT_BUFFER   = 0.05    # 5% above prev_day_low = alert zone
SL_PCT         = 0.04    # 4% SL below prev_day_low
TARGET_PCT     = 0.15    # 15% target above prev_day_low

# ── NSE BHAVCOPY ───────────────────────────────────────────────────────────────
def fetch_bhavcopy(date: datetime) -> pd.DataFrame:
    """Download NSE CM bhavcopy CSV for a given date."""
    headers = {
        'User-Agent': 'Mozilla/5.0',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Referer': 'https://www.nseindia.com/'
    }
    session = requests.Session()
    try:
        session.get('https://www.nseindia.com', headers=headers, timeout=15)
    except:
        pass

    date_str = date.strftime('%d%b%Y').upper()
    
    # Try new URL format first
    url = f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{date.strftime('%Y%m%d')}_F_0000.csv.zip"
    try:
        r = session.get(url, headers=headers, timeout=30)
        if r.status_code == 200 and len(r.content) > 1000:
            z = zipfile.ZipFile(io.BytesIO(r.content))
            fname = z.namelist()[0]
            df = pd.read_csv(z.open(fname))
            return df
    except Exception as e:
        print(f"    New URL failed: {e}")

    # Try legacy URL
    url2 = f"https://nsearchives.nseindia.com/content/historical/EQUITIES/{date.strftime('%Y')}/{date.strftime('%b').upper()}/cm{date_str}bhav.csv.zip"
    try:
        r = session.get(url2, headers=headers, timeout=30)
        if r.status_code == 200 and len(r.content) > 1000:
            z = zipfile.ZipFile(io.BytesIO(r.content))
            fname = z.namelist()[0]
            df = pd.read_csv(z.open(fname))
            return df
    except Exception as e:
        print(f"    Legacy URL failed: {e}")

    return None


def parse_bhavcopy(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise bhavcopy columns, keep only EQ series."""
    if df is None or df.empty:
        return pd.DataFrame()
    
    df.columns = [c.strip() for c in df.columns]

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

    required = {'SYMBOL', 'CLOSE', 'OPEN', 'LOW'}
    missing  = required - set(df.columns)
    if missing:
        return pd.DataFrame()

    if 'SERIES' in df.columns:
        df = df[df['SERIES'].str.strip() == 'EQ'].copy()

    for col in ['CLOSE', 'OPEN', 'LOW', 'VOLUME', 'HIGH', 'PREV']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')

    df['SYMBOL'] = df['SYMBOL'].str.strip().str.upper()
    return df.dropna(subset=['CLOSE', 'OPEN'])


def find_gainers(df: pd.DataFrame, min_gain: float) -> list:
    """Find stocks with close >= min_gain% above prev_close."""
    gainers = []
    for _, row in df.iterrows():
        close = float(row['CLOSE'])
        prev  = float(row['PREV']) if 'PREV' in row.index and pd.notna(row['PREV']) and float(row['PREV']) > 0 else 0

        if prev <= 0 or close <= 0:
            continue

        gain_pct = (close - prev) / prev * 100
        if gain_pct >= min_gain:
            gainers.append({
                'ticker':     str(row['SYMBOL']),
                'close':      round(close, 2),
                'prev_close': round(prev, 2),
                'low':        round(float(row.get('LOW', 0)), 2),
                'gain_pct':   round(gain_pct, 2),
                'volume':     int(row.get('VOLUME', 0)) if 'VOLUME' in row.index else 0,
            })

    return gainers


def get_prev_day_low_yf(tickers: list) -> dict:
    """Fetch prev day low for a list of tickers using yfinance batch download."""
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
        except Exception:
            pass
        time.sleep(0.3)

    return result


def load_watchlist() -> list:
    if os.path.exists(WATCHLIST_FILE):
        try:
            with open(WATCHLIST_FILE) as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
            if isinstance(data, dict) and 'stocks' in data:
                return data['stocks']
        except Exception:
            pass
    return []


def save_watchlist(watchlist: list):
    try:
        with open(WATCHLIST_FILE) as f:
            existing = json.load(f)
        if isinstance(existing, dict) and 'stocks' in existing:
            existing['stocks'] = watchlist
            with open(WATCHLIST_FILE, 'w') as f:
                json.dump(existing, f, indent=2)
            return
    except Exception:
        pass
    with open(WATCHLIST_FILE, 'w') as f:
        json.dump(watchlist, f, indent=2)


def backfill_volume_gainers(days: int, dry_run: bool = False):
    """Scan last N days and backfill volume gainers."""
    print("="*70)
    print(f"VOLUME GAINER BACKFILL - Last {days} Days")
    print(f"Min gain threshold: {MIN_GAIN_PCT}%")
    print(f"Dry run: {dry_run}")
    print("="*70)

    watchlist = load_watchlist()
    existing = {e['ticker']: e['added_date'] for e in watchlist}
    
    print(f"\nCurrent watchlist: {len(watchlist)} stocks")
    print(f"Scanning {days} days of bhavcopy data...\n")

    all_gainers = []
    success_count = 0
    
    for i in range(days):
        target_date = datetime.now() - timedelta(days=i)
        
        # Skip weekends
        if target_date.weekday() >= 5:
            continue
        
        date_str = target_date.strftime('%Y-%m-%d')
        print(f"[{date_str}] ", end='', flush=True)
        
        # Fetch bhavcopy
        raw_df = fetch_bhavcopy(target_date)
        if raw_df is None or raw_df.empty:
            print("❌ No data")
            continue
        
        df = parse_bhavcopy(raw_df)
        if df.empty:
            print("❌ Parse failed")
            continue
        
        gainers = find_gainers(df, MIN_GAIN_PCT)
        if not gainers:
            print(f"✓ No gainers")
            continue
        
        print(f"✅ {len(gainers)} gainers found")
        
        for g in gainers:
            g['scan_date'] = date_str
            all_gainers.append(g)
        
        success_count += 1
        time.sleep(0.5)  # Rate limiting
    
    print(f"\n{'='*70}")
    print(f"Scan complete: {success_count} trading days processed")
    print(f"Total gainers found: {len(all_gainers)}")
    print(f"{'='*70}\n")

    if not all_gainers:
        print("No new gainers to add.")
        return

    # Group by ticker and keep only the most recent occurrence
    by_ticker = {}
    for g in all_gainers:
        ticker = g['ticker']
        if ticker not in by_ticker or g['scan_date'] > by_ticker[ticker]['scan_date']:
            by_ticker[ticker] = g

    unique_gainers = list(by_ticker.values())
    print(f"Unique stocks: {len(unique_gainers)}\n")

    # Fetch prev_day_low for all new tickers
    new_tickers = [g['ticker'] for g in unique_gainers if g['ticker'] not in existing]
    
    if not new_tickers:
        print("All gainers already in watchlist.")
        return
    
    print(f"Fetching prev_day_low for {len(new_tickers)} new stocks via yfinance...")
    prev_lows = get_prev_day_low_yf(new_tickers)
    print(f"Got prev_day_low for {len(prev_lows)}/{len(new_tickers)} stocks\n")

    # Build new entries
    new_entries = []
    for g in unique_gainers:
        sym = g['ticker']
        
        if sym in existing:
            print(f"  ⊘ {sym}: already in watchlist (added {existing[sym]})")
            continue

        prev_low = prev_lows.get(sym) or g.get('low', 0)
        if not prev_low or prev_low <= 0:
            print(f"  ✗ {sym}: no prev_day_low data")
            continue

        entry = {
            'ticker':          sym,
            'company':         sym,
            'added_date':      g['scan_date'],
            'gain_pct':        g['gain_pct'],
            'close_price':     g['close'],
            'prev_close':      g['prev_close'],
            'prev_day_low':    prev_low,
            'alert_threshold': round(prev_low * (1 + ALERT_BUFFER), 2),
            'sl_price':        round(prev_low * (1 - SL_PCT), 2),
            'target_price':    round(prev_low * (1 + TARGET_PCT), 2),
            'vol_ratio':       0.0,
            'alerted':         False,
            'alert_sent_at':   None,
        }

        watchlist.append(entry)
        new_entries.append(entry)
        print(f"  ✅ {sym}: +{g['gain_pct']}% on {g['scan_date']} | "
              f"prev_low=₹{prev_low:,.2f} | target=₹{entry['target_price']:,.2f}")

    print(f"\n{'='*70}")
    print(f"Backfill Summary:")
    print(f"  New stocks added: {len(new_entries)}")
    print(f"  Total watchlist size: {len(watchlist)}")
    print(f"{'='*70}\n")

    if dry_run:
        print("DRY RUN - No changes saved to file.")
    else:
        save_watchlist(watchlist)
        print(f"✅ Saved to {WATCHLIST_FILE}")


def main():
    parser = argparse.ArgumentParser(description='Backfill volume gainers from historical data')
    parser.add_argument('--days', type=int, default=60, help='Number of days to scan (default: 60)')
    parser.add_argument('--dry-run', action='store_true', help='Preview changes without saving')
    
    args = parser.parse_args()
    
    backfill_volume_gainers(args.days, args.dry_run)


if __name__ == '__main__':
    main()
