#!/usr/bin/env python3
"""
Volume Gainer Backfill
======================
First-run utility: scans the last 60 days for ALL stocks in Stock List.csv
and rebuilds the watchlist with historical signals that are still active
(price has NOT yet come back down to the alert zone).

Logic per day per stock:
  1. Gain >10% vs previous day's close  → signal detected
  2. Check subsequent price action: if price has since gone BELOW
     signal_day_low → alert already triggered → skip (expired)
  3. If STILL above (signal_day_low * 1.05) → watchable → add

Only the MOST RECENT signal per ticker is kept to avoid duplicates.

Output: volume_gainer_watchlist.json (merged with any existing entries)
"""

import os
import json
import requests
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta, date

# ── Config ─────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT  = os.environ.get('TELEGRAM_CHAT_ID', '')
WATCHLIST_FILE = 'volume_gainer_watchlist.json'
STOCK_LIST_CSV = 'Stock List.csv'
BASE_URL       = 'https://anuragsin17-sketch.github.io/Stock-Yard-Public'

MIN_GAIN_PCT  = 10.0    # minimum % gain on signal day
ALERT_ZONE    = 0.05    # 5% above signal low = alert zone threshold
SL_PCT        = 0.07    # 7% below signal low = stop-loss
TARGET_PCT    = 0.20    # 20% above signal low = target
LOOKBACK_DAYS = 60      # how many calendar days to look back
BATCH_SIZE    = 40      # tickers per yfinance download batch


# ── Telegram ────────────────────────────────────────────────────────────────
def send_telegram(message: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        print("⚠️  Telegram not configured — skipping notification")
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


# ── Watchlist I/O ───────────────────────────────────────────────────────────
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


# ── Stock list ───────────────────────────────────────────────────────────────
def load_symbols() -> list:
    """Load all symbols from Stock List.csv."""
    csv_path = STOCK_LIST_CSV
    # Also look one directory up (GitHub Actions runs from backend_repo/)
    if not os.path.exists(csv_path):
        csv_path = os.path.join('..', STOCK_LIST_CSV)
    try:
        df = pd.read_csv(csv_path)
        col = next(
            (c for c in df.columns if c.lower() in ('symbol', 'ticker')),
            df.columns[0]
        )
        symbols = df[col].dropna().str.strip().str.upper().tolist()
        print(f"Loaded {len(symbols)} symbols from {csv_path}")
        return symbols
    except Exception as e:
        print(f"Could not load stock list ({csv_path}): {e}")
        raise


# ── Core scan ────────────────────────────────────────────────────────────────
def build_backfill_candidates(symbols: list) -> list:
    """
    For each symbol, download ~LOOKBACK_DAYS + buffer of daily data.
    Walk through each day and detect gain >MIN_GAIN_PCT vs prior close.
    For each signal, check if price later fell below signal_day_low.
    Returns a list of candidate dicts (best/most-recent signal per ticker).
    """
    today = datetime.now().date()
    # Extra buffer so we have enough data for "subsequent price action" check
    start_date = today - timedelta(days=LOOKBACK_DAYS + 10)

    candidates = {}   # ticker → best (most recent) candidate dict
    total = len(symbols)
    processed = 0

    print(f"\nScanning {total} stocks over last {LOOKBACK_DAYS} days (from {start_date})...")
    print(f"Batch size: {BATCH_SIZE}\n")

    for batch_start in range(0, total, BATCH_SIZE):
        batch = symbols[batch_start: batch_start + BATCH_SIZE]
        yf_tickers = [s + '.NS' for s in batch]

        try:
            raw = yf.download(
                yf_tickers,
                start=start_date.strftime('%Y-%m-%d'),
                end=(today + timedelta(days=1)).strftime('%Y-%m-%d'),
                interval='1d',
                group_by='ticker',
                auto_adjust=True,
                progress=False,
                threads=True
            )
        except Exception as e:
            print(f"  ⚠️  Batch {batch_start // BATCH_SIZE + 1} download failed: {e}")
            processed += len(batch)
            continue

        for sym in batch:
            yf_sym = sym + '.NS'
            try:
                # Handle single-ticker vs multi-ticker download format
                if len(batch) == 1:
                    df = raw.copy()
                else:
                    if yf_sym not in raw.columns.get_level_values(0):
                        continue
                    df = raw[yf_sym].copy()

                df = df.dropna(subset=['Close', 'Low'])
                if len(df) < 3:
                    continue

                df = df.sort_index()
                closes = df['Close'].values
                lows   = df['Low'].values
                dates  = [d.date() if hasattr(d, 'date') else d for d in df.index]

                # Walk each day (skip index 0, need previous day)
                for i in range(1, len(df)):
                    signal_date = dates[i]

                    # Only consider days within our LOOKBACK_DAYS window
                    if (today - signal_date).days > LOOKBACK_DAYS:
                        continue

                    prev_close     = float(closes[i - 1])
                    signal_close   = float(closes[i])
                    signal_low     = float(lows[i])

                    if prev_close <= 0 or signal_low <= 0:
                        continue

                    gain_pct = ((signal_close - prev_close) / prev_close) * 100

                    if gain_pct < MIN_GAIN_PCT:
                        continue

                    # --- Signal found for this day ---
                    alert_threshold = signal_low * (1 + ALERT_ZONE)

                    # Check all subsequent days: did price fall below signal_low?
                    expired = False
                    current_price = signal_close  # fallback = signal close

                    subsequent_lows   = lows[i + 1:]
                    subsequent_closes = closes[i + 1:]

                    for j, sub_low in enumerate(subsequent_lows):
                        if float(sub_low) < signal_low:
                            expired = True
                            break
                        current_price = float(subsequent_closes[j])

                    # Most recent close if we have data after signal day
                    if len(subsequent_closes) > 0:
                        current_price = float(subsequent_closes[-1])

                    if expired:
                        continue  # already triggered — not watchable

                    # Still watchable — check if current price is above alert_threshold
                    # (if it dipped into alert zone but not below signal_low it could still fire)
                    # We still keep it as watchable regardless of current vs alert_threshold

                    # Calculate vol ratio (volume on signal day vs prev day)
                    vol_ratio = 0.0
                    if 'Volume' in df.columns:
                        try:
                            sig_vol  = float(df['Volume'].iloc[i])
                            prev_vol = float(df['Volume'].iloc[i - 1])
                            vol_ratio = round(sig_vol / prev_vol, 2) if prev_vol > 0 else 0.0
                        except Exception:
                            pass

                    candidate = {
                        'ticker':          sym,
                        'company':         sym,
                        'added_date':      signal_date.strftime('%Y-%m-%d'),
                        'gain_pct':        round(gain_pct, 2),
                        'close_price':     round(signal_close, 2),
                        'prev_day_low':    round(signal_low, 2),
                        'alert_threshold': round(alert_threshold, 2),
                        'sl_price':        round(signal_low * (1 - SL_PCT), 2),
                        'target_price':    round(signal_low * (1 + TARGET_PCT), 2),
                        'vol_ratio':       vol_ratio,
                        'alerted':         False,
                        'alert_sent_at':   None,
                    }

                    # Keep only the most recent signal per ticker
                    if sym not in candidates or signal_date > datetime.strptime(
                            candidates[sym]['added_date'], '%Y-%m-%d').date():
                        candidates[sym] = candidate

            except Exception as e:
                # Per-ticker errors shouldn't abort the batch
                continue

        processed += len(batch)
        print(f"  Processed {processed}/{total} stocks | active candidates so far: {len(candidates)}")

    result = sorted(candidates.values(), key=lambda x: x['added_date'], reverse=True)
    return result


# ── Merge with existing watchlist ─────────────────────────────────────────────
def merge_into_watchlist(backfill_candidates: list) -> tuple:
    """
    Load existing watchlist and merge:
    - Never overwrite an existing unalerted entry (respect live tracker data)
    - Add new tickers not yet in the watchlist
    Returns (merged_watchlist, new_count)
    """
    existing = load_watchlist()
    existing_tickers = {e['ticker'] for e in existing}

    new_entries = []
    for c in backfill_candidates:
        if c['ticker'] in existing_tickers:
            print(f"  ⊘ {c['ticker']}: already in watchlist — skipping")
            continue
        new_entries.append(c)
        print(f"  ✅ Backfill add: {c['ticker']}  signal={c['added_date']}  "
              f"+{c['gain_pct']}%  prev_low=₹{c['prev_day_low']:,.2f}  "
              f"alert@₹{c['alert_threshold']:,.2f}")

    merged = existing + new_entries
    return merged, len(new_entries)


# ── Telegram summary ──────────────────────────────────────────────────────────
def send_summary(new_entries: list, new_count: int, total_watching: int):
    if new_count == 0:
        msg = (
            f"📋 *VOLUME GAINER BACKFILL COMPLETE*\n"
            f"_{datetime.now().strftime('%d %b %Y, %H:%M IST')}_\n\n"
            f"No new historical signals found (or all already in watchlist).\n"
            f"Total watching: {total_watching}\n\n"
            f"[Open Dashboard]({BASE_URL}/)"
        )
    else:
        # Show up to 15 entries in summary
        lines = []
        for e in new_entries[:15]:
            lines.append(
                f"• *{e['ticker']}* +{e['gain_pct']}% on {e['added_date']} | "
                f"Low ₹{e['prev_day_low']:,.0f} | Alert ₹{e['alert_threshold']:,.0f}"
            )
        more = f"\n_...and {new_count - 15} more_" if new_count > 15 else ""

        msg = (
            f"📈 *VOLUME GAINER BACKFILL COMPLETE*\n"
            f"_{datetime.now().strftime('%d %b %Y, %H:%M IST')}_\n\n"
            f"*{new_count} historical signals added* (last {LOOKBACK_DAYS} days)\n"
            f"Total watching: {total_watching}\n\n"
            + '\n'.join(lines) + more +
            f"\n\n🔔 Alert triggers when price enters 5% above signal day's low.\n"
            f"[Open Dashboard]({BASE_URL}/)"
        )

    send_telegram(msg)
    print(f"\n📨 Telegram summary sent.")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print(f"VOLUME GAINER BACKFILL — {datetime.now().strftime('%Y-%m-%d %H:%M IST')}")
    print(f"Lookback: {LOOKBACK_DAYS} days  |  Min gain: {MIN_GAIN_PCT}%")
    print("=" * 60)

    symbols = load_symbols()
    candidates = build_backfill_candidates(symbols)

    print(f"\n{len(candidates)} active (non-expired) historical signals found.")

    watchlist, new_count = merge_into_watchlist(candidates)
    save_watchlist(watchlist)

    print(f"\n📊 Summary:")
    print(f"  Historical signals found : {len(candidates)}")
    print(f"  New entries added         : {new_count}")
    print(f"  Total in watchlist        : {len(watchlist)}")

    send_summary(candidates[:new_count] if new_count else [], new_count, len(watchlist))

    print("\n✅ Backfill complete.")
    print("=" * 60)


if __name__ == '__main__':
    main()
