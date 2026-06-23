#!/usr/bin/env python3
"""
Volume Gainer Tracker — Full NSE Universe
==========================================
Runs AFTER market close (3:35 PM IST / 10:05 UTC, Mon-Fri).

Strategy:
  1. Download NSE bhavcopy (all ~2000 EQ stocks) for today
  2. Filter: close > prev_close by >= 12%  (uses PREV column from bhavcopy)
  3. Entry price = same day LOW - 2%
  4. Save to volume_gainer_watchlist.json + push to DynamoDB
  5. Send Telegram summary

NO stock list needed — covers every NSE EQ stock automatically.

Watchlist entry:
  {
    "ticker":          "PANAMAPET",
    "company":         "Panama Petrochem Ltd",
    "added_date":      "2026-06-19",
    "gain_pct":        20.0,
    "close_price":     489.9,
    "prev_close":      408.25,
    "prev_day_low":    400.36,   same day LOW * 0.98  (LOW - 2%)
    "alert_threshold": 420.38,   entry * 1.05
    "sl_price":        384.35,   entry * 0.96  (4% SL)
    "target_price":    460.41,   entry * 1.15  (15% target)
    "vol_ratio":       4.2,
    "alerted":         false,
    "alert_sent_at":   null
  }
"""

import os, json, io, zipfile, requests, time
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta

# ── CONFIG ─────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT  = os.environ.get('TELEGRAM_CHAT_ID', '')
WATCHLIST_FILE = 'volume_gainer_watchlist.json'
BASE_URL       = 'https://anuragsin17-sketch.github.io/Stock-Yard-Public'
DYNAMODB_URL   = os.environ.get('DYNAMODB_API_URL', 'https://32-194-58-75.nip.io')

MIN_GAIN_PCT   = 12.0    # minimum % gain on day
ALERT_BUFFER   = 0.05    # 5% above prev_day_low = alert zone
SL_PCT         = 0.04    # 4% SL below prev_day_low
TARGET_PCT     = 0.15    # 15% target above prev_day_low
MAX_STOCKS     = 50      # max per day (safety cap — won't normally hit this)
MIN_PRICE      = 10.0    # minimum stock price (₹10)

# ── TELEGRAM ───────────────────────────────────────────────────────────────────
def send_telegram(message: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        print("⚠️  Telegram not configured"); return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={'chat_id': TELEGRAM_CHAT, 'text': message, 'parse_mode': 'Markdown'},
            timeout=10
        )
        return resp.status_code == 200
    except Exception as e:
        print(f"Telegram error: {e}"); return False

# ── NSE BHAVCOPY ───────────────────────────────────────────────────────────────
def fetch_bhavcopy(date: datetime) -> pd.DataFrame:
    """
    Download NSE CM bhavcopy CSV for a given date.
    Returns DataFrame with columns: SYMBOL, CLOSE, PREVCLOSE, OPEN, LOW, VOLUME, SERIES
    Falls back up to 3 previous trading days if today's isn't available yet.
    """
    headers = {
        'User-Agent': 'Mozilla/5.0',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Referer': 'https://www.nseindia.com/'
    }
    session = requests.Session()
    session.get('https://www.nseindia.com', headers=headers, timeout=15)

    for delta in range(0, 5):
        d = date - timedelta(days=delta)
        if d.weekday() >= 5:   # skip weekends
            continue
        date_str = d.strftime('%d%b%Y').upper()   # e.g. 19JUN2026
        # New NSE URL format
        url = f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{d.strftime('%Y%m%d')}_F_0000.csv.zip"
        try:
            print(f"  Trying bhavcopy: {url}")
            r = session.get(url, headers=headers, timeout=30)
            if r.status_code == 200 and len(r.content) > 1000:
                z = zipfile.ZipFile(io.BytesIO(r.content))
                fname = z.namelist()[0]
                df = pd.read_csv(z.open(fname))
                print(f"  ✅ Bhavcopy loaded: {len(df)} records for {d.strftime('%Y-%m-%d')}")
                return df, d
        except Exception as e:
            print(f"  New URL failed: {e}")

        # Try legacy URL format
        url2 = f"https://nsearchives.nseindia.com/content/historical/EQUITIES/{d.strftime('%Y')}/{d.strftime('%b').upper()}/cm{date_str}bhav.csv.zip"
        try:
            print(f"  Trying legacy: {url2}")
            r = session.get(url2, headers=headers, timeout=30)
            if r.status_code == 200 and len(r.content) > 1000:
                z = zipfile.ZipFile(io.BytesIO(r.content))
                fname = z.namelist()[0]
                df = pd.read_csv(z.open(fname))
                print(f"  ✅ Legacy bhavcopy loaded: {len(df)} records for {d.strftime('%Y-%m-%d')}")
                return df, d
        except Exception as e:
            print(f"  Legacy URL failed: {e}")

    return None, None


def parse_bhavcopy(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise bhavcopy columns, keep only EQ series."""
    # Normalise column names — NSE changed format in 2026 to camelCase
    df.columns = [c.strip() for c in df.columns]

    # Map new NSE format (2026+) and old format column names
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
        print(f"  ⚠️ Missing columns after remap: {missing}. Available: {list(df.columns)}")
        return pd.DataFrame()

    # Keep EQ series only
    if 'SERIES' in df.columns:
        df = df[df['SERIES'].str.strip() == 'EQ'].copy()

    # Ensure numeric
    for col in ['CLOSE', 'OPEN', 'LOW', 'VOLUME', 'HIGH', 'PREV']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')

    df['SYMBOL'] = df['SYMBOL'].str.strip().str.upper()
    return df.dropna(subset=['CLOSE', 'OPEN'])


def find_gainers(df: pd.DataFrame, min_gain: float) -> list:
    """
    Find stocks with close >= min_gain% above prev_close.
    Uses PREVCLOSE if available, else uses previous row (not reliable — we'll
    validate with yfinance for the actual prev close).
    Filters out stocks with close price < MIN_PRICE (₹10).
    """
    gainers = []
    for _, row in df.iterrows():
        close  = float(row['CLOSE'])
        # Use PREV (PrvsClsgPric) for accurate gain calculation — never OPEN
        prev   = float(row['PREV']) if 'PREV' in row.index and pd.notna(row['PREV']) and float(row['PREV']) > 0 else 0

        if prev <= 0 or close <= 0:
            continue

        # Skip stocks below minimum price threshold
        if close < MIN_PRICE:
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

    gainers.sort(key=lambda x: x['gain_pct'], reverse=True)
    print(f"  Found {len(gainers)} stocks with >{min_gain}% gain and price >= ₹{MIN_PRICE}")
    return gainers


def get_prev_day_low_yf(tickers: list) -> dict:
    """
    Fetch prev day low for a list of tickers using yfinance batch download.
    Returns {ticker: prev_day_low}
    """
    result  = {}
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
                        # Second-to-last row = previous trading day
                        result[sym] = round(float(df['Low'].iloc[-2]), 2)
                except Exception:
                    pass
        except Exception as e:
            print(f"  yfinance batch error: {e}")
        time.sleep(0.3)

    return result

# ── WATCHLIST ──────────────────────────────────────────────────────────────────
def load_watchlist() -> list:
    if os.path.exists(WATCHLIST_FILE):
        try:
            with open(WATCHLIST_FILE) as f:
                data = json.load(f)
            # Support both plain array and {last_scan_run, stocks} object
            if isinstance(data, list):
                return data
            if isinstance(data, dict) and 'stocks' in data:
                return data['stocks']
        except Exception:
            pass
    return []

def save_watchlist(watchlist: list, last_run_info: dict = None):
    data = watchlist
    if last_run_info:
        # Wrap in object with metadata so dashboard can read last_scan_run
        data = {'last_scan_run': last_run_info, 'stocks': watchlist}
    else:
        # Check if existing file has metadata wrapper — preserve it
        try:
            with open(WATCHLIST_FILE) as f:
                existing = json.load(f)
            if isinstance(existing, dict) and 'stocks' in existing:
                data = {**existing, 'stocks': watchlist}
        except Exception:
            data = watchlist
    with open(WATCHLIST_FILE, 'w') as f:
        json.dump(data, f, indent=2)
    print(f"✅ Saved {len(watchlist)} stocks to {WATCHLIST_FILE}")

def cleanup_watchlist(watchlist: list, bhavcopy_df: pd.DataFrame) -> tuple:
    """
    Remove expired signals from the watchlist using today's bhavcopy prices.

    Expiry rules:
      1. Target hit  — today's CLOSE >= target_price    → move done, remove
      2. Stale       — added_date older than 30 days    → too old, remove

    Returns (clean_watchlist, removed_list)
    Each removed entry gets a 'removed_reason' field for the Telegram summary.
    """
    # Build quick lookup: ticker → {low, close} from today's bhavcopy
    today_prices = {}
    if bhavcopy_df is not None and not bhavcopy_df.empty:
        for _, row in bhavcopy_df.iterrows():
            sym = str(row.get('SYMBOL', '')).strip().upper()
            if not sym:
                continue
            try:
                today_prices[sym] = {
                    'close': float(row['CLOSE']) if 'CLOSE' in row.index and pd.notna(row['CLOSE']) else None,
                }
            except Exception:
                pass

    cutoff = datetime.now().date() - timedelta(days=30)
    clean   = []
    removed = []

    for entry in watchlist:
        ticker     = (entry.get('ticker') or '').upper()
        target     = float(entry.get('target_price') or 0)
        added_date = entry.get('added_date', '')

        # Check age
        try:
            added = datetime.strptime(added_date, '%Y-%m-%d').date()
        except Exception:
            added = datetime.now().date()

        if added < cutoff:
            entry['removed_reason'] = f'stale (>30d old, added {added_date})'
            removed.append(entry)
            print(f"  🗑  {ticker}: removed — stale signal ({added_date})")
            continue

        prices = today_prices.get(ticker)
        if not prices:
            # Stock not in bhavcopy today (maybe suspended/no trade) — keep it
            clean.append(entry)
            continue

        today_close = prices['close']

        # Target hit — today's close crossed target
        if target > 0 and today_close is not None and today_close >= target:
            entry['removed_reason'] = f'target hit (close ₹{today_close:,.2f} >= target ₹{target:,.2f})'
            removed.append(entry)
            print(f"  🎯 {ticker}: removed — target hit (close={today_close:.2f} >= target={target:.2f})")
            continue

        clean.append(entry)

    return clean, removed


def push_to_dynamodb(new_entries: list):
    """Push new entries to DynamoDB via EC2 API."""
    if not new_entries:
        return
    try:
        r = requests.post(
            f"{DYNAMODB_URL}/api/save-radar",
            json={'signal_type': 'VOLUME', 'stocks': new_entries},
            timeout=15, verify=False
        )
        if r.status_code == 200:
            print(f"  ✅ Pushed {len(new_entries)} entries to DynamoDB")
        else:
            print(f"  ⚠️ DynamoDB push failed: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"  ⚠️ DynamoDB push error: {e}")

# ── MAIN ───────────────────────────────────────────────────────────────────────
def main():
    print("=" * 65)
    print(f"VOLUME GAINER TRACKER (Full NSE) — {datetime.now().strftime('%Y-%m-%d %H:%M IST')}")
    print(f"Min gain: {MIN_GAIN_PCT}%  |  SL: {SL_PCT*100:.0f}%  |  Target: {TARGET_PCT*100:.0f}%")
    print("=" * 65)

    today = datetime.now()

    # ── Step 1: Download NSE bhavcopy ─────────────────────────────────────────
    print("\n[1] Downloading NSE bhavcopy...")
    raw_df, trade_date = fetch_bhavcopy(today)
    if raw_df is None:
        print("❌ Could not download bhavcopy — aborting")
        return

    date_str = trade_date.strftime('%Y-%m-%d')
    print(f"  Trade date: {date_str}")

    # ── Step 2: Parse and filter gainers ──────────────────────────────────────
    print("\n[2] Parsing bhavcopy...")
    df = parse_bhavcopy(raw_df)
    if df.empty:
        print("❌ Bhavcopy parse returned empty DataFrame — aborting")
        return
    print(f"  {len(df)} EQ stocks parsed")

    print(f"\n[3] Cleaning up expired signals from existing watchlist...")
    watchlist_before_cleanup = load_watchlist()
    print(f"  Watchlist before cleanup: {len(watchlist_before_cleanup)} stocks")
    cleaned_watchlist, expired_entries = cleanup_watchlist(watchlist_before_cleanup, df)
    print(f"  Removed: {len(expired_entries)} | Remaining: {len(cleaned_watchlist)}")

    if expired_entries:
        # Persist the cleaned watchlist immediately so stale entries don't linger
        save_watchlist(cleaned_watchlist)
        # Telegram notification for removed stocks
        tgt_hit    = [e for e in expired_entries if 'target hit'  in e.get('removed_reason','')]
        stale      = [e for e in expired_entries if 'stale'       in e.get('removed_reason','')]
        lines = []
        for e in tgt_hit:
            lines.append(f"🎯 *{e['ticker']}* — {e['removed_reason']}")
        for e in stale:
            lines.append(f"🗑 *{e['ticker']}* — {e['removed_reason']}")
        if lines:
            send_telegram(
                f"🧹 *WATCHLIST CLEANUP — {date_str}*\n"
                f"_{len(expired_entries)} signals expired_\n\n"
                + '\n'.join(lines[:20])
                + (f"\n_...and {len(lines)-20} more_" if len(lines) > 20 else "")
            )

    print(f"\n[4] Finding stocks with >{MIN_GAIN_PCT}% gain...")
    gainers = find_gainers(df, MIN_GAIN_PCT)
    if not gainers:
        print("  No qualifying gainers today")
        send_telegram(f"📊 *Volume Gainer Tracker*\n_{date_str}_\n\nNo stocks gained >{MIN_GAIN_PCT}% today.")
        return

    # Cap at MAX_STOCKS
    gainers = gainers[:MAX_STOCKS]
    print(f"\n  Top gainers (up to {MAX_STOCKS}):")
    for g in gainers[:10]:
        print(f"    {g['ticker']:15} +{g['gain_pct']:5.1f}%  close=₹{g['close']:,.2f}  prev=₹{g['prev_close']:,.2f}")
    if len(gainers) > 10:
        print(f"    ... and {len(gainers)-10} more")

    # ── Step 3: Merge into watchlist ──────────────────────────────────────────
    print("\n[4] Merging into watchlist...")
    watchlist = load_watchlist()
    existing  = {e['ticker'] for e in watchlist}
    new_entries = []

    for g in gainers:
        sym = g['ticker']
        if sym in existing:
            print(f"  ⊘ {sym}: already in watchlist — skipping")
            continue

        # Entry price = same day LOW - 2%
        same_day_low = g.get('low', 0)
        if not same_day_low or same_day_low <= 0:
            print(f"  ✗ {sym}: no same-day LOW data — skipping")
            continue
        
        entry_price = round(same_day_low * 0.98, 2)  # Same day LOW - 2%

        entry = {
            'ticker':          sym,
            'company':         sym,
            'added_date':      date_str,
            'gain_pct':        g['gain_pct'],
            'close_price':     g['close'],
            'prev_close':      g['prev_close'],
            'prev_day_low':    entry_price,           # Changed: now same day LOW - 2%
            'alert_threshold': round(entry_price * (1 + ALERT_BUFFER), 2),
            'sl_price':        round(entry_price * (1 - SL_PCT), 2),
            'target_price':    round(entry_price * (1 + TARGET_PCT), 2),
            'vol_ratio':       0.0,
            'alerted':         False,
            'alert_sent_at':   None,
        }

        # Fetch 52W H/L for display (best-effort)
        try:
            t52   = yf.Ticker(sym + '.NS')
            h52   = t52.history(period='1y', interval='1d', auto_adjust=False)
            if not h52.empty:
                entry['week_52_high'] = round(float(h52['High'].max()), 2)
                entry['week_52_low']  = round(float(h52['Low'].min()),  2)
        except Exception:
            pass

        watchlist.append(entry)
        new_entries.append(entry)
        print(f"  ✅ {sym}: +{g['gain_pct']}%  entry=₹{entry_price:,.2f} (LOW-2%)  "
              f"target=₹{entry['target_price']:,.2f}  sl=₹{entry['sl_price']:,.2f}")

    save_watchlist(watchlist, last_run_info={
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M IST'),
        'date': date_str,
        'new_stocks': len(new_entries),
        'total': len(watchlist)
    })

    # ── Step 5: Push to DynamoDB ──────────────────────────────────────────────
    if new_entries:
        print(f"\n[6] Pushing {len(new_entries)} new entries to DynamoDB...")
        push_to_dynamodb(new_entries)

    # ── Step 6: Telegram summary ──────────────────────────────────────────────
    if new_entries:
        lines = []
        for e in new_entries[:15]:
            lines.append(
                f"• *{e['ticker']}* +{e['gain_pct']}% | "
                f"Entry: ₹{e['prev_day_low']:,.0f} | "
                f"Target: ₹{e['target_price']:,.0f} | "
                f"SL: ₹{e['sl_price']:,.0f}"
            )
        msg = (
            f"📈 *VOLUME GAINER WATCHLIST — {date_str}*\n"
            f"_{len(new_entries)} new stocks added (gained >{MIN_GAIN_PCT}%)_\n\n"
            + '\n'.join(lines)
            + (f"\n\n_...and {len(new_entries)-15} more_" if len(new_entries) > 15 else "")
            + f"\n\n🔔 Entry = Same day LOW - 2%\n"
            f"[Open Dashboard]({BASE_URL}/)"
        )
        send_telegram(msg)
        print(f"\n✅ Done — {len(new_entries)} new stocks added, {len(watchlist)} total in watchlist")
    else:
        print(f"\n✅ Done — 0 new stocks added (all already in watchlist)")

    print("=" * 65)


if __name__ == '__main__':
    main()
