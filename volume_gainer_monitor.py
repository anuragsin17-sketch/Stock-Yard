#!/usr/bin/env python3
"""
Volume Gainer Monitor
=====================
Runs DURING market hours every 30 min (Mon-Fri, 9:15-15:30 IST).

What it does:
  1. Loads volume_gainer_watchlist.json
  2. For each unalerted stock, fetches live LTP via EC2 /api/get-quote
     (falls back to yfinance if EC2 unavailable)
  3. If LTP <= alert_threshold (within 5% of prev day's low) → send Telegram alert
  4. Marks stock as alerted so no repeat spam
  5. Keeps alerted stocks in watchlist for history — they are not removed

Alert message includes:
  - Stock name, current price, prev day low
  - % distance from prev day low
  - How much it gained on the day it was added
  - TradingView chart link + dashboard link
  - Confirm Trade button

Alert dedup:
  - Once alerted=True, never alerts again for same stock/event
  - Watchlist persists across days — new entries added each evening by tracker
"""

import os
import json
import requests
import yfinance as yf
from datetime import datetime

TELEGRAM_TOKEN  = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT   = os.environ.get('TELEGRAM_CHAT_ID', '')
WATCHLIST_FILE  = 'volume_gainer_watchlist.json'
EC2_QUOTE_URL   = 'https://32-194-58-75.nip.io/api/get-quote'
BASE_URL        = 'https://anuragsin17-sketch.github.io/Stock-Yard-Public'
ALERT_BUFFER    = 0.05   # alert when within 5% above prev day low
SL_PCT          = 0.07   # 7% stop loss below entry
TARGET_PCT      = 0.20   # 20% target above entry


def send_telegram(message: str, reply_markup: dict = None) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        print("⚠️  Telegram not configured")
        return False
    try:
        payload = {
            'chat_id':    TELEGRAM_CHAT,
            'text':       message,
            'parse_mode': 'Markdown'
        }
        if reply_markup:
            payload['reply_markup'] = reply_markup
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json=payload,
            timeout=10
        )
        if resp.status_code == 200:
            print("✅ Telegram sent")
            return True
        print(f"❌ Telegram failed {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"❌ Telegram error: {e}")
    return False


def get_ltp_ec2(ticker: str) -> float:
    """Fetch live LTP from EC2 Angel One API."""
    try:
        resp = requests.get(EC2_QUOTE_URL, params={'symbol': ticker}, timeout=8)
        if resp.status_code == 200:
            data = resp.json()
            if data.get('success'):
                ltp = float(data.get('ltp', 0))
                if ltp > 0:
                    return ltp
    except Exception as e:
        print(f"  EC2 quote error for {ticker}: {e}")
    return None


def get_ltp_yfinance(ticker: str) -> float:
    """Fallback LTP from yfinance."""
    try:
        t = yf.Ticker(ticker + '.NS')
        hist = t.history(period='1d', interval='5m')
        if not hist.empty:
            return float(hist['Close'].iloc[-1])
    except Exception as e:
        print(f"  yfinance error for {ticker}: {e}")
    return None


def get_ltp(ticker: str) -> float:
    """Get LTP — try EC2 first, fall back to yfinance."""
    ltp = get_ltp_ec2(ticker)
    if ltp:
        return ltp
    print(f"  EC2 failed for {ticker}, trying yfinance...")
    return get_ltp_yfinance(ticker)


def load_watchlist() -> list:
    if os.path.exists(WATCHLIST_FILE):
        try:
            with open(WATCHLIST_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return []


def save_watchlist(watchlist: list):
    with open(WATCHLIST_FILE, 'w') as f:
        json.dump(watchlist, f, indent=2)


def build_alert_message(watch: dict, ltp: float) -> tuple:
    """Returns (message_str, reply_markup_dict)"""
    ticker       = watch['ticker']
    prev_low     = watch['prev_day_low']
    threshold    = watch['alert_threshold']
    gain_pct     = watch['gain_pct']
    added_date   = watch['added_date']

    dist_pct = ((ltp - prev_low) / prev_low) * 100

    target_price = watch.get('target_price') or round(prev_low * (1 + TARGET_PCT), 2)
    stop_price   = watch.get('sl_price')     or round(prev_low * (1 - SL_PCT), 2)

    qty          = max(1, int(50000 / ltp))
    confirm_url  = (
        f"{BASE_URL}/?confirm={ticker}"
        f"&price={ltp}&qty={qty}"
        f"&stop={stop_price}&target={target_price}&source=VolumeGainer"
    )
    chart_url = f"https://in.tradingview.com/chart/?symbol=NSE:{ticker}"

    msg = (
        f"🔔 *VOLUME GAINER — PREV LOW ZONE*\n\n"
        f"📈 *{ticker}*\n"
        f"💰 Current LTP: ₹{ltp:,.2f}\n"
        f"📉 Prev Day Low: ₹{prev_low:,.2f}\n"
        f"📊 Distance from Low: *{dist_pct:+.1f}%*\n\n"
        f"📅 Added: {added_date} _(gained +{gain_pct}% that day)_\n"
        f"🎯 Target: ₹{target_price:,.2f} | 🛑 SL: ₹{stop_price:,.2f}\n\n"
        f"_Price is within 5% of previous day low — potential retest entry._\n\n"
        f"[📉 Chart]({chart_url}) | [📱 Dashboard]({BASE_URL}/)"
    )

    buttons = {
        'inline_keyboard': [[
            {'text': '✅ Confirm Trade', 'url': confirm_url},
            {'text': '📉 View Chart',    'url': chart_url}
        ]]
    }

    return msg, buttons


def check_watchlist():
    watchlist = load_watchlist()
    if not watchlist:
        print("Watchlist is empty — nothing to monitor")
        return

    pending = [e for e in watchlist if not e.get('alerted', False)]
    print(f"Monitoring {len(pending)} unalerted stocks ({len(watchlist)} total in watchlist)")

    alerts_sent = 0
    for entry in watchlist:
        if entry.get('alerted', False):
            continue

        ticker    = entry['ticker']
        threshold = entry['alert_threshold']  # prev_low * 1.05
        prev_low  = entry['prev_day_low']

        ltp = get_ltp(ticker)
        if not ltp:
            print(f"  {ticker}: could not get LTP — skipping")
            continue

        dist_pct = ((ltp - prev_low) / prev_low) * 100
        print(f"  {ticker}: LTP=₹{ltp:,.2f}  prev_low=₹{prev_low:,.2f}  dist={dist_pct:+.1f}%  threshold=₹{threshold:,.2f}")

        if ltp <= threshold:
            # Price is within 5% of prev day low — ALERT
            print(f"  🔔 {ticker}: IN ALERT ZONE ({dist_pct:+.1f}% from prev low) — sending alert")
            msg, buttons = build_alert_message(entry, ltp)
            if send_telegram(msg, reply_markup=buttons):
                entry['alerted']       = True
                entry['alert_sent_at'] = datetime.now().isoformat()
                entry['alert_ltp']     = ltp
                entry['alert_dist_pct'] = round(dist_pct, 2)
                alerts_sent += 1
        else:
            print(f"  {ticker}: not in zone yet ({dist_pct:+.1f}% above prev low, need ≤5%)")

    save_watchlist(watchlist)
    print(f"\n✅ Done — {alerts_sent} alert(s) sent")


def print_watchlist_status():
    """Print full watchlist status for debugging."""
    watchlist = load_watchlist()
    if not watchlist:
        print("Watchlist empty.")
        return
    print(f"\n{'─'*70}")
    print(f"{'TICKER':<12} {'ADDED':^12} {'GAIN%':>6} {'PREV_LOW':>10} {'THRESHOLD':>10} {'ALERTED':>8}")
    print(f"{'─'*70}")
    for e in sorted(watchlist, key=lambda x: x['added_date'], reverse=True):
        alerted = '✅' if e.get('alerted') else '⏳'
        print(
            f"{e['ticker']:<12} {e['added_date']:^12} {e['gain_pct']:>5.1f}% "
            f"₹{e['prev_day_low']:>9,.0f} ₹{e['alert_threshold']:>9,.0f} {alerted:>8}"
        )
    print(f"{'─'*70}\n")


def main():
    print("=" * 60)
    print(f"VOLUME GAINER MONITOR — {datetime.now().strftime('%Y-%m-%d %H:%M IST')}")
    print("=" * 60)

    print_watchlist_status()
    check_watchlist()

    print("=" * 60)


if __name__ == '__main__':
    main()
