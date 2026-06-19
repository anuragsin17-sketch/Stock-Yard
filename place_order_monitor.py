#!/usr/bin/env python3
"""
Place Order Monitor
===================
Single source of truth for all Place Order tab entry/exit Telegram alerts.
Runs as a systemd service on EC2, checking every 5 minutes during market hours.

Rules:
  - ENTRY alert: stock enters -10% to +2% of entry trigger → "Added to Place Order tab"
  - EXIT alert:  stock leaves zone after being in it → "Exited Place Order tab"
  - NO duplicates: state tracked in place_order_state.json
  - Covers BOTH: Trendline (trendline_screen.json) and Volume (volume_gainer_watchlist.json)
"""

import os
import json
import time
import requests
from datetime import datetime, time as dtime

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo('Asia/Kolkata')
except ImportError:
    try:
        import pytz
        IST = pytz.timezone('Asia/Kolkata')
    except ImportError:
        IST = None

TELEGRAM_TOKEN   = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT    = os.environ.get('TELEGRAM_CHAT_ID', '')
TRENDLINE_FILE   = '/home/ubuntu/stock-yard-backend/trendline_screen.json'
VOLUME_FILE      = '/home/ubuntu/stock-yard-backend/volume_gainer_watchlist.json'
STATE_FILE       = '/home/ubuntu/place_order_state.json'
APP_URL          = 'https://anuragsin17-sketch.github.io/Stock-Yard-Public'
ENTRY_ZONE_LOW   = -10.0   # -10% below trigger
ENTRY_ZONE_HIGH  =   2.0   # +2% above trigger
CHECK_INTERVAL   = 300     # 5 minutes
MARKET_OPEN      = dtime(9, 15)
MARKET_CLOSE     = dtime(15, 35)

# When run from GitHub Actions the EC2 state file is absent.
# Skip alerting in that case to avoid re-sending alerts the EC2 service already sent.
RUNNING_AS_SERVICE = (
    os.path.exists(STATE_FILE) or
    os.environ.get('PLACE_ORDER_SERVICE_MODE', '') == '1'
)


def _now_ist():
    if IST:
        return datetime.now(tz=IST)
    return datetime.now()


def is_market_hours():
    now = _now_ist().time()
    return MARKET_OPEN <= now <= MARKET_CLOSE


def send_telegram(msg: str, buttons: dict = None) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        return False
    try:
        payload = {'chat_id': TELEGRAM_CHAT, 'text': msg, 'parse_mode': 'Markdown'}
        if buttons:
            payload['reply_markup'] = buttons
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json=payload, timeout=10
        )
        return r.status_code == 200
    except Exception as e:
        print(f"  Telegram error: {e}")
    return False


def get_ltps(tickers: list) -> dict:
    """Fetch LTPs via DynamoDB bulk endpoint, fallback to yfinance batch."""
    prices = {}
    EC2_PRICES_URL = 'https://32-194-58-75.nip.io/api/prices'

    # Primary: DynamoDB bulk endpoint
    try:
        resp = requests.get(EC2_PRICES_URL,
                           params={'tickers': ','.join(tickers)},
                           timeout=10, verify=False)
        if resp.status_code == 200:
            data = resp.json()
            if data.get('success') and data.get('prices'):
                for ticker, ltp in data['prices'].items():
                    if ltp and float(ltp) > 0:
                        prices[ticker.upper()] = round(float(ltp), 2)
        if prices:
            print(f"  DynamoDB bulk: {len(prices)} prices")
            return prices
    except Exception as e:
        print(f"  DynamoDB bulk error: {e}")

    # Fallback: yfinance batch (works everywhere including GitHub Actions)
    print("  Falling back to yfinance batch...")
    try:
        import yfinance as yf
        symbols = [t + '.NS' for t in tickers]
        data = yf.download(symbols, period='2d', interval='1d',
                           auto_adjust=True, progress=False, threads=True)
        if not data.empty and 'Close' in data:
            last = data['Close'].iloc[-1]
            for sym in symbols:
                val = last.get(sym)
                if val and float(val) > 0:
                    prices[sym.replace('.NS', '')] = round(float(val), 2)
        if prices:
            print(f"  yfinance batch: {len(prices)} prices")
    except Exception as e:
        print(f"  yfinance batch error: {e}")

    return prices


def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: dict):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


def entry_msg(ticker, strategy, ltp, trigger, dist_pct, target, sl, extra=''):
    chart = f"https://in.tradingview.com/chart/?symbol=NSE:{ticker}"
    app   = f"{APP_URL}/"
    sign  = '+' if dist_pct >= 0 else ''
    return (
        f"🟢 *PLACE ORDER — ENTRY*\n\n"
        f"📊 *{ticker}* ({strategy})\n"
        f"💰 LTP: ₹{ltp:,.2f} | Entry: ₹{trigger:,.2f} ({sign}{dist_pct:.1f}%)\n"
        f"🎯 Target: ₹{target:,.2f} | 🛑 SL: ₹{sl:,.2f}\n"
        f"{extra}"
        f"\n_Within entry zone (-10% to +2%) — ready to buy._\n\n"
        f"[📉 Chart]({chart}) | [📱 App]({app})"
    ), {
        'inline_keyboard': [[
            {'text': '📱 Open App',   'url': app},
            {'text': '📉 View Chart', 'url': chart}
        ]]
    }


def exit_msg(ticker, strategy, ltp, trigger, dist_pct):
    sign = '+' if dist_pct >= 0 else ''
    direction = "moved up ↑" if dist_pct > ENTRY_ZONE_HIGH else "dropped further ↓"
    return (
        f"🔴 *PLACE ORDER — EXIT*\n\n"
        f"📊 *{ticker}* ({strategy})\n"
        f"💰 LTP: ₹{ltp:,.2f} | Entry: ₹{trigger:,.2f} ({sign}{dist_pct:.1f}%)\n\n"
        f"_Price {direction} — removed from Place Order tab._"
    )


def load_candidates() -> list:
    candidates = []

    # Trendline stocks
    for path in [TRENDLINE_FILE, 'trendline_screen.json']:
        if os.path.exists(path):
            try:
                with open(path) as f:
                    tl_stocks = json.load(f)
                for s in (tl_stocks or []):
                    ticker  = s.get('ticker') or s.get('symbol') or ''
                    trigger = s.get('triggerPrice') or 0
                    if not ticker or not trigger:
                        continue
                    ps     = s.get('positionSizing', {})
                    target = ps.get('pivotTargetExit') or round(float(trigger) * 1.23, 2)
                    sl     = ps.get('strictStopLoss')  or round(float(trigger) * 0.92, 2)
                    wicks  = s.get('wickTouches') or 0
                    score  = s.get('confluenceScore') or 0
                    extra  = f"📐 Score: {score} | Wicks: {wicks}\n" if (score or wicks) else ''
                    candidates.append({
                        'ticker': ticker.upper(), 'strategy': 'Trendline',
                        'trigger': float(trigger), 'target': float(target),
                        'sl': float(sl), 'extra': extra
                    })
                break
            except Exception as e:
                print(f"  Trendline file error: {e}")

    # Volume stocks
    for path in [VOLUME_FILE, 'volume_gainer_watchlist.json']:
        if os.path.exists(path):
            try:
                with open(path) as f:
                    vol_stocks = json.load(f)
                for s in (vol_stocks or []):
                    ticker  = s.get('ticker') or ''
                    trigger = s.get('prev_day_low') or 0
                    if not ticker or not trigger:
                        continue
                    target = s.get('target_price') or round(float(trigger) * 1.20, 2)
                    sl     = s.get('sl_price')     or round(float(trigger) * 0.93, 2)
                    gain   = s.get('gain_pct') or 0
                    added  = s.get('added_date') or ''
                    extra  = f"📅 Added: {added} _(+{gain:.1f}% signal day)_\n" if added else ''
                    candidates.append({
                        'ticker': ticker.upper(), 'strategy': 'Volume',
                        'trigger': float(trigger), 'target': float(target),
                        'sl': float(sl), 'extra': extra
                    })
                break
            except Exception as e:
                print(f"  Volume file error: {e}")

    return candidates


def check_once():
    print(f"\n{'='*50}")
    print(f"PLACE ORDER CHECK — {_now_ist().strftime('%H:%M:%S IST')}")

    if not RUNNING_AS_SERVICE:
        print("  Not running as EC2 service — skipping alerts to avoid duplicates with the service.")
        return

    candidates = load_candidates()
    if not candidates:
        print("No candidates — skipping")
        return

    tickers = list({c['ticker'] for c in candidates})
    ltp_map = get_ltps(tickers)
    print(f"Prices: {len(ltp_map)}/{len(tickers)} fetched")

    state      = load_state()
    entry_sent = 0
    exit_sent  = 0

    # Deduplicate candidates by ticker — if the same ticker appears in both
    # Trendline and Volume sources, only keep one (Trendline takes priority).
    seen_tickers: dict = {}
    for c in candidates:
        tk = c['ticker']
        if tk not in seen_tickers or c['strategy'] == 'Trendline':
            seen_tickers[tk] = c
    deduped_candidates = list(seen_tickers.values())

    for c in deduped_candidates:
        ticker   = c['ticker']
        trigger  = c['trigger']
        ltp = ltp_map.get(ticker)
        if not ltp:
            continue

        dist_pct  = (ltp - trigger) / trigger * 100
        in_zone   = ENTRY_ZONE_LOW <= dist_pct <= ENTRY_ZONE_HIGH
        # State keyed by ticker only — prevents double-alerting same stock
        # from two strategies (Trendline + Volume).
        state_key = ticker
        prev      = state.get(state_key, {})
        was_in    = prev.get('in_zone', False)

        if in_zone and not was_in:
            msg, buttons = entry_msg(ticker, c['strategy'], ltp, trigger, dist_pct,
                                     c['target'], c['sl'], c['extra'])
            if send_telegram(msg, buttons):
                state[state_key] = {'in_zone': True, 'strategy': c['strategy'],
                                    'entry_at': _now_ist().isoformat(), 'entry_ltp': ltp}
                entry_sent += 1
                print(f"  🟢 ENTRY: {ticker} ({c['strategy']}) dist={dist_pct:+.1f}%")

        elif not in_zone and was_in:
            msg = exit_msg(ticker, c['strategy'], ltp, trigger, dist_pct)
            if send_telegram(msg):
                state[state_key] = {'in_zone': False, 'strategy': c['strategy'],
                                    'exit_at': _now_ist().isoformat(), 'exit_ltp': ltp}
                exit_sent += 1
                print(f"  🔴 EXIT:  {ticker} ({c['strategy']}) dist={dist_pct:+.1f}%")
        else:
            state[state_key] = {**prev, 'in_zone': in_zone, 'last_ltp': ltp,
                                 'last_check': _now_ist().isoformat()}

    save_state(state)
    print(f"Done — {entry_sent} entries, {exit_sent} exits sent")


def main():
    print("Place Order Monitor starting...")
    print(f"Checking every {CHECK_INTERVAL//60} min during market hours (IST)")

    while True:
        try:
            if is_market_hours():
                check_once()
            else:
                print(f"  Outside market hours ({_now_ist().strftime('%H:%M IST')}) — sleeping")
        except Exception as e:
            print(f"  Loop error: {e}")
        time.sleep(CHECK_INTERVAL)


if __name__ == '__main__':
    main()

