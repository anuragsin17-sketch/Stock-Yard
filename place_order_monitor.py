#!/usr/bin/env python3
"""
Place Order Monitor
===================
Single source of truth for all Place Order tab entry/exit Telegram alerts.
Runs every 30 min during market hours via GitHub Actions (same schedule as volume monitor).

Rules:
  - ENTRY alert: stock enters ±2% of its entry trigger → "Added to Place Order tab"
  - EXIT alert:  stock leaves ±2% zone after being in it → "Exited Place Order tab"
  - NO duplicates: state tracked in place_order_state.json
  - Covers BOTH strategies: Trendline (trendline_screen.json) and Volume (volume_gainer_watchlist.json)
"""

import os
import json
import requests
import yfinance as yf
from datetime import datetime

TELEGRAM_TOKEN   = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT    = os.environ.get('TELEGRAM_CHAT_ID', '')
TRENDLINE_FILE   = 'trendline_screen.json'
VOLUME_FILE      = 'volume_gainer_watchlist.json'
STATE_FILE       = 'place_order_state.json'
APP_URL          = 'https://anuragsin17-sketch.github.io/Stock-Yard-Public'
ENTRY_ZONE_PCT   = 2.0   # ±2% of entry trigger


# ── Telegram ─────────────────────────────────────────────────────────────────

def send_telegram(msg: str, buttons: dict = None) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        print("⚠️  Telegram not configured")
        return False
    try:
        payload = {'chat_id': TELEGRAM_CHAT, 'text': msg, 'parse_mode': 'Markdown'}
        if buttons:
            payload['reply_markup'] = buttons
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json=payload, timeout=10
        )
        if r.status_code == 200:
            print("  ✅ Telegram sent")
            return True
        print(f"  ❌ Telegram failed {r.status_code}: {r.text[:100]}")
    except Exception as e:
        print(f"  ❌ Telegram error: {e}")
    return False


# ── Price fetch ───────────────────────────────────────────────────────────────

def get_ltps(tickers: list) -> dict:
    """Batch fetch LTPs via yfinance. Returns {TICKER: ltp}."""
    prices = {}
    try:
        symbols = [t + '.NS' for t in tickers]
        data = yf.download(symbols, period='2d', interval='1d',
                           auto_adjust=True, progress=False, threads=True)
        if not data.empty and 'Close' in data:
            last = data['Close'].iloc[-1]
            for sym in symbols:
                val = last.get(sym)
                if val and float(val) > 0:
                    prices[sym.replace('.NS', '')] = round(float(val), 2)
    except Exception as e:
        print(f"  yfinance batch error: {e}")
    return prices


# ── State persistence ─────────────────────────────────────────────────────────

def load_state() -> dict:
    """Load {ticker: {'in_zone': bool, 'entry_at': iso, 'exit_at': iso}}"""
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: dict):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


# ── Alert messages ────────────────────────────────────────────────────────────

def entry_msg(ticker, strategy, ltp, trigger, dist_pct, target, sl, extra=''):
    chart = f"https://in.tradingview.com/chart/?symbol=NSE:{ticker}"
    app   = f"{APP_URL}/"
    return (
        f"🟢 *PLACE ORDER — ENTRY*\n\n"
        f"📊 *{ticker}* ({strategy})\n"
        f"💰 LTP: ₹{ltp:,.2f} | Entry: ₹{trigger:,.2f} ({dist_pct:+.1f}%)\n"
        f"🎯 Target: ₹{target:,.2f} | 🛑 SL: ₹{sl:,.2f}\n"
        f"{extra}"
        f"\n_Within ±2% of entry zone — ready to buy._\n\n"
        f"[📉 Chart]({chart}) | [📱 App]({app})"
    ), {
        'inline_keyboard': [[
            {'text': '📱 Open App',   'url': app},
            {'text': '📉 View Chart', 'url': chart}
        ]]
    }


def exit_msg(ticker, strategy, ltp, trigger, dist_pct):
    direction = "moved up ↑" if dist_pct > ENTRY_ZONE_PCT else "dropped ↓"
    return (
        f"🔴 *PLACE ORDER — EXIT*\n\n"
        f"📊 *{ticker}* ({strategy})\n"
        f"💰 LTP: ₹{ltp:,.2f} | Entry: ₹{trigger:,.2f} ({dist_pct:+.1f}%)\n\n"
        f"_Price {direction} — removed from Place Order tab._"
    )


# ── Build candidate list ──────────────────────────────────────────────────────

def load_candidates() -> list:
    """
    Returns list of dicts:
      {ticker, strategy, trigger, target, sl, extra}
    """
    candidates = []

    # 1. Trendline stocks
    try:
        with open(TRENDLINE_FILE) as f:
            tl_stocks = json.load(f)
        for s in (tl_stocks or []):
            ticker  = s.get('ticker') or s.get('symbol') or ''
            trigger = s.get('triggerPrice') or s.get('trendlinePrice') or 0
            if not ticker or not trigger:
                continue
            ps      = s.get('positionSizing', {})
            target  = ps.get('pivotTargetExit') or round(trigger * 1.23, 2)
            sl      = ps.get('strictStopLoss')  or round(trigger * 0.92, 2)
            wicks   = s.get('wickTouches') or s.get('trendline_touches') or 0
            score   = s.get('confluenceScore') or 0
            extra   = f"📐 Score: {score} | Wicks: {wicks}\n" if (score or wicks) else ''
            candidates.append({
                'ticker': ticker.upper(), 'strategy': 'Trendline',
                'trigger': float(trigger), 'target': float(target),
                'sl': float(sl), 'extra': extra
            })
    except Exception as e:
        print(f"  Trendline file error: {e}")

    # 2. Volume stocks
    try:
        with open(VOLUME_FILE) as f:
            vol_stocks = json.load(f)
        for s in (vol_stocks or []):
            ticker  = s.get('ticker') or ''
            trigger = s.get('prev_day_low') or 0
            if not ticker or not trigger:
                continue
            target  = s.get('target_price') or round(trigger * 1.20, 2)
            sl      = s.get('sl_price')     or round(trigger * 0.93, 2)
            gain    = s.get('gain_pct') or 0
            added   = s.get('added_date') or ''
            extra   = f"📅 Added: {added} _(+{gain:.1f}% signal day)_\n" if added else ''
            candidates.append({
                'ticker': ticker.upper(), 'strategy': 'Volume',
                'trigger': float(trigger), 'target': float(target),
                'sl': float(sl), 'extra': extra
            })
    except Exception as e:
        print(f"  Volume file error: {e}")

    return candidates


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print(f"PLACE ORDER MONITOR — {datetime.now().strftime('%Y-%m-%d %H:%M IST')}")
    print("=" * 60)

    candidates = load_candidates()
    if not candidates:
        print("No candidates loaded — exiting")
        return

    tickers = list({c['ticker'] for c in candidates})
    print(f"Fetching prices for {len(tickers)} tickers...")
    ltp_map = get_ltps(tickers)
    print(f"Got {len(ltp_map)} prices")

    state      = load_state()
    entry_sent = 0
    exit_sent  = 0

    for c in candidates:
        ticker   = c['ticker']
        strategy = c['strategy']
        trigger  = c['trigger']
        target   = c['target']
        sl       = c['sl']
        extra    = c['extra']

        ltp = ltp_map.get(ticker)
        if not ltp:
            print(f"  {ticker} ({strategy}): no price — skip")
            continue

        dist_pct    = (ltp - trigger) / trigger * 100
        in_zone     = abs(dist_pct) <= ENTRY_ZONE_PCT
        state_key   = f"{ticker}_{strategy}"
        prev_state  = state.get(state_key, {})
        was_in_zone = prev_state.get('in_zone', False)

        status = '✅ IN ZONE' if in_zone else '⬜ OUT'
        print(f"  {ticker} ({strategy}): LTP=₹{ltp:,.2f}  trigger=₹{trigger:,.2f}  "
              f"dist={dist_pct:+.1f}%  {status}")

        if in_zone and not was_in_zone:
            # ── ENTRY ──────────────────────────────────────────────────
            msg, buttons = entry_msg(ticker, strategy, ltp, trigger, dist_pct, target, sl, extra)
            if send_telegram(msg, buttons):
                state[state_key] = {
                    'in_zone':  True,
                    'entry_at': datetime.now().isoformat(),
                    'entry_ltp': ltp,
                }
                entry_sent += 1

        elif not in_zone and was_in_zone:
            # ── EXIT ───────────────────────────────────────────────────
            msg = exit_msg(ticker, strategy, ltp, trigger, dist_pct)
            if send_telegram(msg):
                state[state_key] = {
                    'in_zone': False,
                    'exit_at': datetime.now().isoformat(),
                    'exit_ltp': ltp,
                }
                exit_sent += 1
        else:
            # No change — just keep state current
            state[state_key] = {**prev_state, 'in_zone': in_zone, 'last_ltp': ltp}

    save_state(state)
    print(f"\n✅ Done — {entry_sent} entry alert(s), {exit_sent} exit alert(s)")
    print("=" * 60)


if __name__ == '__main__':
    main()
