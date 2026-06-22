#!/usr/bin/env python3
"""
Volume Gainer Monitor
=====================
Runs DURING market hours every 30 min (Mon-Fri, 9:15-15:30 IST).

What it does:
  1. Loads volume_gainer_watchlist.json
  2. For each stock, fetches live LTP
  3. ENTRY alert: LTP within ±2% of prev_day_low → "Added to Place Order tab"
  4. EXIT alert:  LTP moves back outside ±2% after being in zone → "Exited Place Order tab"
  5. Dedupes alerts — once entry alert sent, won't resend until exit+re-entry cycle
"""

import os
import json
import requests
import yfinance as yf
from datetime import datetime
from decimal import Decimal

TELEGRAM_TOKEN  = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT   = os.environ.get('TELEGRAM_CHAT_ID', '')
WATCHLIST_FILE  = 'volume_gainer_watchlist.json'
EC2_QUOTE_URL   = 'https://32-194-58-75.nip.io/api/get-quote'
BASE_URL        = 'https://anuragsin17-sketch.github.io/Stock-Yard-Public'
ALERT_BUFFER    = 0.05   # alert when within 5% above prev day low
SL_PCT          = 0.04   # 4% stop loss below entry
TARGET_PCT      = 0.15   # 15% target above entry
ENTRY_ZONE_PCT  = 0.02   # ±2% of prev_day_low = Place Order zone
ENTRY_ZONE_LOW  = -0.10  # -10%  \  show in Volume tab
ENTRY_ZONE_HIGH =  0.05  #  +5%  /


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


def bulk_fetch_ltps(tickers: list) -> dict:
    """Fetch LTPs for all tickers in one batch — much faster than one-by-one.
    
    Strategy:
    1. Try EC2 DynamoDB bulk endpoint (fastest — single HTTPS call)
    2. Fall back to yfinance batch download (all tickers in one call, ~15s)
    Returns {TICKER: ltp} dict.
    """
    prices = {}
    EC2_PRICES_URL = 'https://32-194-58-75.nip.io/api/prices'

    # Primary: EC2 DynamoDB bulk prices endpoint
    try:
        resp = requests.get(EC2_PRICES_URL,
                            params={'tickers': ','.join(tickers)},
                            timeout=12, verify=False)
        if resp.status_code == 200:
            data = resp.json()
            if data.get('success') and data.get('prices'):
                for ticker, ltp in data['prices'].items():
                    if ltp and float(ltp) > 0:
                        prices[ticker.upper()] = round(float(ltp), 2)
        if len(prices) >= len(tickers) * 0.8:   # got at least 80% — good enough
            print(f"  EC2 bulk: {len(prices)}/{len(tickers)} prices")
            return prices
    except Exception as e:
        print(f"  EC2 bulk error: {e}")

    # Fallback: yfinance batch (all tickers in a single download call)
    print(f"  yfinance batch fallback for {len(tickers)} tickers...")
    try:
        symbols = [t + '.NS' for t in tickers]
        data = yf.download(symbols, period='2d', interval='1d',
                           auto_adjust=True, progress=False, threads=True)
        if not data.empty and 'Close' in data:
            last_row = data['Close'].iloc[-1]
            for sym in symbols:
                val = last_row.get(sym) if hasattr(last_row, 'get') else last_row.get(sym, None)
                if val is not None and float(val) > 0:
                    prices[sym.replace('.NS', '')] = round(float(val), 2)
        print(f"  yfinance batch: {len(prices)}/{len(tickers)} prices")
    except Exception as e:
        print(f"  yfinance batch error: {e}")
        # Last resort: individual yfinance per missing ticker
        missing = [t for t in tickers if t not in prices]
        print(f"  Individual fallback for {len(missing)} missing tickers...")
        for ticker in missing:
            ltp = get_ltp_yfinance(ticker)
            if ltp:
                prices[ticker] = ltp

    return prices


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


def build_entry_alert(watch: dict, ltp: float) -> tuple:
    """Returns (message_str, reply_markup_dict) for ENTRY into Place Order zone."""
    ticker       = watch['ticker']
    prev_low     = watch['prev_day_low']
    gain_pct     = watch['gain_pct']
    added_date   = watch['added_date']
    dist_pct     = ((ltp - prev_low) / prev_low) * 100
    target_price = watch.get('target_price') or round(prev_low * (1 + TARGET_PCT), 2)
    stop_price   = watch.get('sl_price')     or round(prev_low * (1 - SL_PCT), 2)
    app_url      = f"{BASE_URL}/"
    chart_url    = f"https://in.tradingview.com/chart/?symbol=NSE:{ticker}"

    msg = (
        f"🟢 *PLACE ORDER — ENTRY ZONE*\n\n"
        f"📊 *{ticker}* (Volume)\n"
        f"💰 LTP: ₹{ltp:,.2f} | Entry: ₹{prev_low:,.2f} ({dist_pct:+.1f}%)\n"
        f"🎯 Target: ₹{target_price:,.2f} (+{TARGET_PCT*100:.0f}%)\n"
        f"🛑 SL: ₹{stop_price:,.2f} (-{SL_PCT*100:.0f}%)\n\n"
        f"📅 Added: {added_date} _(+{gain_pct:.1f}% signal day)_\n\n"
        f"_Stock is within ±2% of signal day low — ready to buy._\n\n"
        f"[📉 Chart]({chart_url}) | [📱 Open App]({app_url})"
    )
    buttons = {
        'inline_keyboard': [[
            {'text': '📱 Open App',   'url': app_url},
            {'text': '📉 View Chart', 'url': chart_url}
        ]]
    }
    return msg, buttons


def build_exit_alert(watch: dict, ltp: float) -> str:
    """Returns message string when stock exits the Place Order zone."""
    ticker   = watch['ticker']
    prev_low = watch['prev_day_low']
    dist_pct = ((ltp - prev_low) / prev_low) * 100
    direction = "moved up ↑" if dist_pct > 0 else "dropped below ↓"
    return (
        f"🔴 *PLACE ORDER — EXIT*\n\n"
        f"📊 *{ticker}* (Volume)\n"
        f"💰 LTP: ₹{ltp:,.2f} | Entry: ₹{prev_low:,.2f} ({dist_pct:+.1f}%)\n\n"
        f"_Price has {direction} the ±2% entry zone._"
    )


# Keep legacy function name for backward compatibility
def build_alert_message(watch: dict, ltp: float) -> tuple:
    return build_entry_alert(watch, ltp)


def check_watchlist():
    watchlist = load_watchlist()
    if not watchlist:
        print("Watchlist is empty — nothing to monitor")
        return

    print(f"Monitoring {len(watchlist)} stocks")

    # ── Bulk fetch all LTPs in one call (replaces serial one-by-one fetches) ──
    all_tickers = [e['ticker'] for e in watchlist if e.get('ticker')]
    print(f"Bulk fetching LTPs for {len(all_tickers)} tickers...")
    ltp_map = bulk_fetch_ltps(all_tickers)
    print(f"Got {len(ltp_map)} LTPs")

    entry_sent  = 0
    exit_sent   = 0
    db_updates  = []

    for entry in watchlist:
        ticker   = entry['ticker']
        prev_low = entry['prev_day_low']

        ltp = ltp_map.get(ticker.upper())
        if not ltp:
            print(f"  {ticker}: no LTP in bulk result — skipping")
            continue

        dist_pct      = ((ltp - prev_low) / prev_low) * 100
        in_entry_zone = ENTRY_ZONE_LOW * 100 <= dist_pct <= ENTRY_ZONE_HIGH * 100  # -10% to +5%
        in_zone       = abs(dist_pct) <= ENTRY_ZONE_PCT * 100   # ±2% for Telegram alert
        was_in_zone   = entry.get('in_place_order_zone', False)

        print(f"  {ticker}: LTP=₹{ltp:,.2f}  prev_low=₹{prev_low:,.2f}  dist={dist_pct:+.1f}%  "
              f"entry_zone={'✅' if in_entry_zone else '❌'}  alert_zone={'✅' if in_zone else '❌'}")

        # ── Always update LTP + zone flag ────────────────────────────────────
        entry['ltp']            = round(ltp, 2)
        entry['dist_pct']       = round(dist_pct, 2)
        entry['in_entry_zone']  = in_entry_zone
        entry['ltp_updated_at'] = datetime.now().isoformat()
        db_updates.append(entry)

        # ── Telegram alerts (unchanged logic) ────────────────────────────────
        if in_zone and not was_in_zone:
            print(f"  🟢 {ticker}: ENTERED alert zone → sending entry alert")
            msg, buttons = build_entry_alert(entry, ltp)
            if send_telegram(msg, reply_markup=buttons):
                entry['in_place_order_zone']   = True
                entry['place_order_entry_at']  = datetime.now().isoformat()
                entry['place_order_entry_ltp'] = ltp
                entry['alerted']               = True
                entry['alert_sent_at']         = entry['place_order_entry_at']
                entry['alert_ltp']             = ltp
                entry['alert_dist_pct']        = round(dist_pct, 2)
                entry_sent += 1
        elif not in_zone and was_in_zone:
            print(f"  🔴 {ticker}: EXITED alert zone → sending exit alert")
            msg = build_exit_alert(entry, ltp)
            if send_telegram(msg):
                entry['in_place_order_zone'] = False
                entry['place_order_exit_at'] = datetime.now().isoformat()
                entry['place_order_exit_ltp'] = ltp
                exit_sent += 1
        else:
            entry['in_place_order_zone'] = in_zone

    save_watchlist(watchlist)

    # ── Bulk upsert LTP + in_entry_zone flag to DynamoDB ─────────────────────
    if db_updates:
        upsert_to_dynamodb(db_updates)

    print(f"\n✅ Done — {entry_sent} entry alert(s), {exit_sent} exit alert(s), {len(db_updates)} DB updates")


def upsert_to_dynamodb(entries: list):
    """Upsert ltp + in_entry_zone flag for each entry into DynamoDB StockSignals."""
    try:
        import boto3
        AWS_REGION = os.environ.get('AWS_REGION', 'us-east-1')
        db    = boto3.resource('dynamodb', region_name=AWS_REGION)
        table = db.Table('StockSignals')

        def dec(v):
            if isinstance(v, float): return Decimal(str(round(v, 6)))
            if isinstance(v, dict):  return {k: dec(x) for k, x in v.items()}
            if isinstance(v, list):  return [dec(x) for x in v]
            return v

        with table.batch_writer() as batch:
            for e in entries:
                ticker = (e.get('ticker') or '').upper()
                if not ticker: continue
                item = dec({k: v for k, v in e.items() if v is not None})
                item['signal_type'] = 'VOLUME'
                item['ticker']      = ticker
                item['updated_at']  = datetime.utcnow().isoformat()
                batch.put_item(Item=item)

        zone_count = sum(1 for e in entries if e.get('in_entry_zone'))
        print(f"  ✅ DynamoDB: updated {len(entries)} stocks ({zone_count} in entry zone)")
    except Exception as e:
        print(f"  ⚠️ DynamoDB upsert error: {e}")


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
