#!/usr/bin/env python3
"""
Volume Gainer Backfill — Last 2 Months
=======================================
Downloads NSE bhavcopy for every trading day in the past 60 days,
finds all stocks that closed >10% above prev close,
and upserts them into:
  1. volume_gainer_watchlist.json  (local)
  2. DynamoDB StockSignals table   (via dynamodb_helper)

Run once on EC2:
    python3 volume_backfill_2months.py

Or trigger via GitHub Actions workflow_dispatch.
"""

import os, io, json, zipfile, time, requests
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
from decimal import Decimal

MIN_GAIN_PCT  = 10.0
ALERT_BUFFER  = 0.05   # 5% above prev_day_low = alert zone
SL_PCT        = 0.04   # 4% SL
TARGET_PCT    = 0.15   # 15% target
WATCHLIST_FILE = 'volume_gainer_watchlist.json'
BACKFILL_DAYS  = 60    # how many calendar days back to scan

# ── NSE bhavcopy download ─────────────────────────────────────────────────────
def _session():
    s = requests.Session()
    s.headers.update({'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.nseindia.com/'})
    try: s.get('https://www.nseindia.com', timeout=10)
    except: pass
    return s

def fetch_bhavcopy_for_date(d: datetime, session) -> pd.DataFrame:
    """Download and parse NSE bhavcopy for a specific date. Returns EQ DataFrame or None."""
    # Try new format first, then legacy
    urls = [
        f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{d.strftime('%Y%m%d')}_F_0000.csv.zip",
        f"https://nsearchives.nseindia.com/content/historical/EQUITIES/{d.strftime('%Y')}/{d.strftime('%b').upper()}/cm{d.strftime('%d%b%Y').upper()}bhav.csv.zip",
    ]
    for url in urls:
        try:
            r = session.get(url, timeout=20)
            if r.status_code == 200 and len(r.content) > 500:
                z = zipfile.ZipFile(io.BytesIO(r.content))
                df = pd.read_csv(z.open(z.namelist()[0]))
                df.columns = [c.strip().upper().replace(' ', '_') for c in df.columns]

                # Normalise columns
                rename = {}
                for target, cands in {
                    'SYMBOL': ['SYMBOL','TCKRSYMBOL'],
                    'CLOSE':  ['CLOSE','CLOSE_PRICE','CL'],
                    'PREV':   ['PREVCLOSE','PREV_CLOSE','PCLOSE','PREV_CL'],
                    'OPEN':   ['OPEN','OPEN_PRICE','OP'],
                    'LOW':    ['LOW','LOW_PRICE','LO'],
                    'HIGH':   ['HIGH','HIGH_PRICE','HI'],
                    'VOLUME': ['TOTTRDQTY','VOLUME','TTL_TRD_QNTY'],
                    'SERIES': ['SERIES','SRS'],
                }.items():
                    for c in cands:
                        if c in df.columns:
                            rename[c] = target; break
                df = df.rename(columns=rename)

                if 'SERIES' in df.columns:
                    df = df[df['SERIES'].str.strip() == 'EQ'].copy()

                for col in ['CLOSE','OPEN','LOW','PREV']:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors='coerce')

                df['SYMBOL'] = df['SYMBOL'].str.strip().str.upper()
                df = df.dropna(subset=['CLOSE','OPEN'])
                return df
        except Exception:
            continue
    return None


def find_gainers_in_df(df: pd.DataFrame, date_str: str) -> list:
    """Return list of gainer dicts from a parsed bhavcopy DataFrame."""
    gainers = []
    for _, row in df.iterrows():
        close = float(row['CLOSE'])
        # Prefer PREVCLOSE, fall back to OPEN
        prev  = float(row['PREV']) if ('PREV' in row.index and pd.notna(row.get('PREV')) and row.get('PREV', 0) > 0) \
                else float(row['OPEN'])
        if prev <= 0 or close <= 0:
            continue
        gain_pct = (close - prev) / prev * 100
        if gain_pct >= MIN_GAIN_PCT:
            gainers.append({
                'ticker':     str(row['SYMBOL']),
                'added_date': date_str,
                'gain_pct':   round(gain_pct, 2),
                'close_price': round(close, 2),
                'prev_close':  round(prev, 2),
                'low':         round(float(row.get('LOW', 0) or 0), 2),
            })
    gainers.sort(key=lambda x: x['gain_pct'], reverse=True)
    return gainers


def get_prev_lows_yf(ticker_dates: list) -> dict:
    """
    Batch-fetch prev_day_low for unique tickers.
    ticker_dates: list of (ticker, date_str)
    Returns {ticker: {date_str: prev_low}}
    """
    tickers = list(set(t for t, _ in ticker_dates))
    print(f"  Fetching prev_day_low for {len(tickers)} unique tickers via yfinance...")

    result = {}   # {ticker: DataFrame}
    BATCH = 50
    ns_tickers = [t + '.NS' for t in tickers]

    for i in range(0, len(ns_tickers), BATCH):
        batch_ns  = ns_tickers[i:i+BATCH]
        batch_sym = tickers[i:i+BATCH]
        try:
            data = yf.download(
                batch_ns, period='65d', interval='1d',
                auto_adjust=True, progress=False,
                group_by='ticker', threads=True
            )
            for sym, ns in zip(batch_sym, batch_ns):
                try:
                    df = data[ns] if len(batch_ns) > 1 else data
                    df = df.dropna(subset=['Low'])
                    result[sym] = df[['Low']].copy()
                except Exception:
                    pass
        except Exception as e:
            print(f"  yfinance batch error: {e}")
        time.sleep(0.3)

    # Build {ticker: {date_str: prev_low}}
    out = {}
    for sym, df in result.items():
        out[sym] = {}
        df = df.sort_index()
        for i in range(1, len(df)):
            d_str = df.index[i].strftime('%Y-%m-%d')
            out[sym][d_str] = round(float(df['Low'].iloc[i-1]), 2)  # prev day's low

    return out


# ── DynamoDB upsert ───────────────────────────────────────────────────────────
def upsert_to_dynamodb(entries: list):
    """Upsert individual entries to DynamoDB (doesn't wipe existing records)."""
    try:
        import boto3
        from decimal import Decimal
        from datetime import datetime

        def _dec(obj):
            if isinstance(obj, float): return Decimal(str(round(obj, 6)))
            if isinstance(obj, dict):  return {k: _dec(v) for k, v in obj.items()}
            if isinstance(obj, list):  return [_dec(i) for i in obj]
            return obj

        db    = boto3.resource('dynamodb', region_name=os.environ.get('AWS_REGION','us-east-1'))
        table = db.Table('StockSignals')
        written = 0

        with table.batch_writer() as batch:
            for e in entries:
                ticker = e.get('ticker','').upper()
                if not ticker: continue
                item = _dec(dict(e))
                item['signal_type'] = 'VOLUME'
                item['ticker']      = ticker
                item['updated_at']  = datetime.utcnow().isoformat()
                batch.put_item(Item=item)
                written += 1

        print(f"  ✅ DynamoDB: upserted {written} VOLUME signals")
        return written
    except Exception as e:
        print(f"  ⚠️ DynamoDB upsert failed: {e}")
        return 0


# ── Watchlist helpers ─────────────────────────────────────────────────────────
def load_watchlist() -> list:
    if os.path.exists(WATCHLIST_FILE):
        try:
            with open(WATCHLIST_FILE) as f:
                d = json.load(f)
            return d if isinstance(d, list) else []
        except Exception: pass
    return []

def save_watchlist(wl: list):
    with open(WATCHLIST_FILE, 'w') as f:
        json.dump(wl, f, indent=2)


# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    print("=" * 65)
    print(f"VOLUME BACKFILL — Last {BACKFILL_DAYS} days")
    print(f"Min gain: {MIN_GAIN_PCT}%  |  SL: {SL_PCT*100:.0f}%  |  Target: {TARGET_PCT*100:.0f}%")
    print("=" * 65)

    today  = datetime.now()
    cutoff = today - timedelta(days=BACKFILL_DAYS)
    session = _session()

    # ── Step 1: Walk every trading day and collect gainers ────────────────────
    all_gainers = []   # list of gainer dicts
    day = today - timedelta(days=1)  # start from yesterday

    print(f"\n[1] Scanning {BACKFILL_DAYS} days back ({cutoff.date()} → {today.date()})...")

    while day >= cutoff:
        if day.weekday() >= 5:    # skip weekends
            day -= timedelta(days=1)
            continue

        date_str = day.strftime('%Y-%m-%d')
        print(f"\n  {date_str}", end=' ', flush=True)

        df = fetch_bhavcopy_for_date(day, session)
        if df is None or df.empty:
            print("— no data")
            day -= timedelta(days=1)
            time.sleep(0.5)
            continue

        gainers = find_gainers_in_df(df, date_str)
        print(f"— {len(df)} EQ stocks  |  {len(gainers)} gainers >10%")
        all_gainers.extend(gainers)

        day -= timedelta(days=1)
        time.sleep(0.3)   # be gentle with NSE archives

    print(f"\n  Total gainer records across all days: {len(all_gainers)}")

    if not all_gainers:
        print("Nothing to backfill.")
        return

    # ── Step 2: Batch-fetch prev_day_low via yfinance ─────────────────────────
    print(f"\n[2] Fetching prev_day_low via yfinance...")
    ticker_dates = [(g['ticker'], g['added_date']) for g in all_gainers]
    prev_lows = get_prev_lows_yf(ticker_dates)

    # ── Step 3: Build full entries ────────────────────────────────────────────
    print(f"\n[3] Building watchlist entries...")
    watchlist    = load_watchlist()
    existing_ids = {(e['ticker'], e['added_date']) for e in watchlist}
    new_entries  = []

    for g in all_gainers:
        key = (g['ticker'], g['added_date'])
        if key in existing_ids:
            continue   # already have this one

        # Get prev_day_low
        prev_low = (prev_lows.get(g['ticker'], {}).get(g['added_date'])
                    or g.get('low') or 0)
        if prev_low <= 0:
            continue

        entry = {
            'ticker':          g['ticker'],
            'company':         g['ticker'],
            'added_date':      g['added_date'],
            'gain_pct':        g['gain_pct'],
            'close_price':     g['close_price'],
            'prev_close':      g['prev_close'],
            'prev_day_low':    prev_low,
            'alert_threshold': round(prev_low * (1 + ALERT_BUFFER), 2),
            'sl_price':        round(prev_low * (1 - SL_PCT),    2),
            'target_price':    round(prev_low * (1 + TARGET_PCT), 2),
            'vol_ratio':       0.0,
            'alerted':         False,
            'alert_sent_at':   None,
        }
        new_entries.append(entry)
        existing_ids.add(key)

    print(f"  New entries to add: {len(new_entries)}")

    # ── Step 4: Save to watchlist ─────────────────────────────────────────────
    watchlist.extend(new_entries)
    # Sort newest first
    watchlist.sort(key=lambda x: x.get('added_date',''), reverse=True)
    save_watchlist(watchlist)
    print(f"  ✅ Watchlist saved: {len(watchlist)} total entries")

    # ── Step 5: Push to DynamoDB ──────────────────────────────────────────────
    print(f"\n[4] Upserting {len(new_entries)} entries to DynamoDB...")
    written = upsert_to_dynamodb(new_entries)

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'='*65}")
    print(f"  BACKFILL COMPLETE")
    print(f"  Days scanned    : {BACKFILL_DAYS} calendar days")
    print(f"  Gainer records  : {len(all_gainers)}")
    print(f"  New entries     : {len(new_entries)}")
    print(f"  DynamoDB writes : {written}")
    print(f"  Watchlist total : {len(watchlist)}")
    print(f"{'='*65}\n")


if __name__ == '__main__':
    main()
