#!/usr/bin/env python3
"""
Volume Gainer Tracker
=====================
Runs AFTER market close (3:35 PM IST / ~10:05 UTC, Mon-Fri).

What it does:
  1. Fetches all Nifty 500 stocks via yfinance
  2. Finds top 15 that gained >10% TODAY on high volume
  3. Records their previous day's LOW price as the watch level
  4. Saves to volume_gainer_watchlist.json (persists across days)
  5. Sends Telegram summary of new stocks added to watchlist

Watchlist format per entry:
  {
    "ticker": "CRAFTSMAN",
    "company": "Craftsman Automation",
    "added_date": "2026-06-14",
    "gain_pct": 13.5,
    "close_price": 9096.5,
    "prev_day_low": 7800.0,       ← this is the watch target
    "alert_threshold": 8190.0,    ← prev_day_low * 1.05 (5% above = alert zone entry)
    "alerted": false,
    "alert_sent_at": null
  }
"""

import os
import json
import requests
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT  = os.environ.get('TELEGRAM_CHAT_ID', '')
WATCHLIST_FILE = 'volume_gainer_watchlist.json'
NIFTY500_FILE  = 'Stock List.csv'
BASE_URL       = 'https://anuragsin17-sketch.github.io/Stock-Yard-Public'

MIN_GAIN_PCT   = 10.0   # minimum % gain to qualify
TOP_N          = 15     # max stocks to track
ALERT_BUFFER   = 0.05   # 5% above prev low = alert zone


def send_telegram(message: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        print("⚠️  Telegram not configured")
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={'chat_id': TELEGRAM_CHAT, 'text': message, 'parse_mode': 'Markdown'},
            timeout=10
        )
        return resp.status_code == 200
    except Exception as e:
        print(f"Telegram error: {e}")
        return False


def load_watchlist() -> list:
    if os.path.exists(WATCHLIST_FILE):
        try:
            with open(WATCHLIST_FILE) as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception:
            pass
    return []


def save_watchlist(watchlist: list):
    with open(WATCHLIST_FILE, 'w') as f:
        json.dump(watchlist, f, indent=2)
    print(f"✅ Saved {len(watchlist)} stocks to {WATCHLIST_FILE}")


def load_nifty500_symbols() -> list:
    """Load Nifty 500 tickers from CSV."""
    try:
        df = pd.read_csv(NIFTY500_FILE)
        # Column could be 'Symbol' or 'ticker'
        col = next((c for c in df.columns if c.lower() in ('symbol', 'ticker')), df.columns[0])
        symbols = df[col].dropna().str.strip().str.upper().tolist()
        print(f"Loaded {len(symbols)} symbols from {NIFTY500_FILE}")
        return symbols
    except Exception as e:
        print(f"Could not load {NIFTY500_FILE}: {e}")
        # Fallback — Nifty 50 only
        return [
            'RELIANCE','TCS','HDFCBANK','INFY','ICICIBANK','HINDUNILVR','ITC',
            'SBIN','BHARTIARTL','KOTAKBANK','LT','ASIANPAINT','AXISBANK','MARUTI',
            'TITAN','SUNPHARMA','BAJFINANCE','NESTLEIND','WIPRO','ULTRACEMCO',
            'ONGC','NTPC','POWERGRID','HCLTECH','TECHM','INDUSINDBK','GRASIM',
            'ADANIPORTS','BAJAJFINSV','JSWSTEEL'
        ]


def fetch_todays_gainers(symbols: list) -> list:
    """
    For each symbol, fetch last 3 days of data.
    Calculate today's gain vs yesterday's close.
    Return list of dicts sorted by gain desc, filtered >10%.
    """
    gainers = []
    total = len(symbols)
    print(f"Scanning {total} stocks for today's top gainers...")

    batch_size = 50
    for i in range(0, total, batch_size):
        batch = symbols[i:i+batch_size]
        yf_tickers = [s + '.NS' for s in batch]

        try:
            # Download last 5 days to ensure we always have prev day data
            data = yf.download(
                yf_tickers,
                period='5d',
                interval='1d',
                group_by='ticker',
                auto_adjust=True,
                progress=False,
                threads=True
            )
        except Exception as e:
            print(f"  Batch {i//batch_size + 1} download failed: {e}")
            continue

        for sym in batch:
            yf_sym = sym + '.NS'
            try:
                # Handle single vs multi ticker download format
                if len(batch) == 1:
                    df = data
                else:
                    if yf_sym not in data.columns.get_level_values(0):
                        continue
                    df = data[yf_sym]

                df = df.dropna(subset=['Close'])
                if len(df) < 2:
                    continue

                today_close  = float(df['Close'].iloc[-1])
                prev_close   = float(df['Close'].iloc[-2])
                prev_low     = float(df['Low'].iloc[-2])    # previous day's low
                today_open   = float(df['Open'].iloc[-1])
                today_volume = float(df['Volume'].iloc[-1]) if 'Volume' in df.columns else 0
                prev_volume  = float(df['Volume'].iloc[-2]) if 'Volume' in df.columns else 1

                gain_pct = ((today_close - prev_close) / prev_close) * 100

                if gain_pct >= MIN_GAIN_PCT and prev_low > 0:
                    gainers.append({
                        'ticker':       sym,
                        'gain_pct':     round(gain_pct, 2),
                        'close_price':  round(today_close, 2),
                        'prev_close':   round(prev_close, 2),
                        'prev_day_low': round(prev_low, 2),
                        'vol_ratio':    round(today_volume / prev_volume, 2) if prev_volume > 0 else 0,
                    })

            except Exception as e:
                continue

        print(f"  Processed {min(i+batch_size, total)}/{total} stocks, gainers so far: {len(gainers)}")

    # Sort by gain descending, take top N
    gainers.sort(key=lambda x: x['gain_pct'], reverse=True)
    return gainers[:TOP_N]


def update_watchlist(new_gainers: list) -> tuple:
    """
    Merge new gainers into existing watchlist.
    - Keep existing entries (not yet alerted) even if stock not in today's gainers
    - Add new gainers not already in list
    - Remove entries where alert has been sent AND stock is no longer a gainer
      (keeps watchlist clean over time)
    Returns (watchlist, new_count)
    """
    watchlist = load_watchlist()
    existing_tickers = {e['ticker'] for e in watchlist}
    today_str = datetime.now().strftime('%Y-%m-%d')

    new_count = 0
    for g in new_gainers:
        if g['ticker'] in existing_tickers:
            # Already watching — skip (don't reset alert state)
            print(f"  ⊘ {g['ticker']}: already in watchlist")
            continue

        alert_threshold = round(g['prev_day_low'] * (1 + ALERT_BUFFER), 2)
        entry = {
            'ticker':          g['ticker'],
            'company':         g['ticker'],  # yfinance doesn't give name in bulk — ticker is fine
            'added_date':      today_str,
            'gain_pct':        g['gain_pct'],
            'close_price':     g['close_price'],
            'prev_close':      g['prev_close'],
            'prev_day_low':    g['prev_day_low'],
            'alert_threshold': alert_threshold,  # LTP <= this → send alert
            'vol_ratio':       g['vol_ratio'],
            'alerted':         False,
            'alert_sent_at':   None
        }
        watchlist.append(entry)
        new_count += 1
        print(f"  ✅ Added {g['ticker']}: gain={g['gain_pct']}%, prev_low=₹{g['prev_day_low']}, alert_at=₹{alert_threshold}")

    return watchlist, new_count


def send_summary(new_gainers: list, new_count: int, total_watching: int):
    if not new_gainers:
        print("No new gainers >10% today — no Telegram summary sent")
        return

    lines = []
    for g in new_gainers[:10]:
        lines.append(
            f"• *{g['ticker']}* +{g['gain_pct']}% | "
            f"Prev Low: ₹{g['prev_day_low']:,.0f} | "
            f"Vol: {g['vol_ratio']}x"
        )

    msg = (
        f"📈 *TOP GAINERS WATCHLIST UPDATE*\n"
        f"_{datetime.now().strftime('%d %b %Y, %H:%M IST')}_\n\n"
        f"*{new_count} new stocks added* (gained >10% today)\n"
        f"Total watching: {total_watching}\n\n"
        + '\n'.join(lines) +
        f"\n\n🔔 Alert triggers when price falls within 5% of previous day's low.\n"
        f"[Open Dashboard]({BASE_URL}/)"
    )
    send_telegram(msg)


def main():
    print("=" * 60)
    print(f"VOLUME GAINER TRACKER — {datetime.now().strftime('%Y-%m-%d %H:%M IST')}")
    print("=" * 60)

    symbols = load_nifty500_symbols()
    print(f"\nFetching today's gainers (>={MIN_GAIN_PCT}%)...")
    new_gainers = fetch_todays_gainers(symbols)
    print(f"\nFound {len(new_gainers)} stocks with >{MIN_GAIN_PCT}% gain today:")
    for g in new_gainers:
        print(f"  {g['ticker']:15} +{g['gain_pct']:5.1f}%  prev_low=₹{g['prev_day_low']:,.2f}")

    watchlist, new_count = update_watchlist(new_gainers)
    save_watchlist(watchlist)
    send_summary(new_gainers, new_count, len(watchlist))

    print(f"\n✅ Done. Watchlist has {len(watchlist)} stocks to monitor.")
    print("=" * 60)


if __name__ == '__main__':
    main()
