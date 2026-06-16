#!/usr/bin/env python3
"""
Fib Price Alert — 5-Minute Live Monitor
========================================
Runs every 5 minutes via EC2 crontab during market hours.

Logic:
  1. Load trendline_cache.json  (has _weekly/_monthly fib grids per stock)
  2. Batch-fetch live prices from yfinance (1-day data — fast, no API key needed)
     Falls back to Angel One SmartAPI if available
  3. For each stock check THREE conditions:
       A. Price inside W/M 61.8% + 50% ultra-pocket  → score 10 → CRITICAL alert
       B. Price inside W/M 61.8% pocket              → score 9  → HIGH alert
       C. Price inside W/M 50% pocket                → score 8  → MEDIUM alert
       D. Price within 2% of trendline (regardless)  → trendline touch alert
  4. Send Telegram — once per stock per session (deduped via alerted_5min.json)
  5. Runs only during market hours: 09:15–15:30 IST Mon–Fri

Setup on EC2 (add to crontab):
  */5 9-15 * * 1-5 cd /home/ubuntu/Stock-Yard && python fib_price_alert.py >> logs/fib_alert.log 2>&1

Dependencies:
  pip install yfinance requests pyotp SmartApi-python  (already installed on EC2)
"""

import os, json, time, pyotp, requests
from datetime import datetime, date
import yfinance as yf
import warnings
warnings.filterwarnings('ignore')

# ─── CONFIG ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT    = os.environ.get('TELEGRAM_CHAT_ID', '')
APP_URL          = 'https://anuragsin17-sketch.github.io/Stock-Yard-Public'
CACHE_FILE       = 'trendline_cache.json'
ALERT_STATE_FILE = 'alerted_5min.json'  # resets daily

# Alert thresholds
SCORE_CRITICAL   = 10   # inside BOTH 61.8% AND 50% pocket
SCORE_HIGH       = 9    # inside 61.8% pocket
SCORE_MEDIUM     = 8    # inside 50% pocket
TL_TOUCH_PCT     = 3.0  # within 3% of trendline  → extra alert

# Cooldown — don't re-alert same stock for same level for N minutes
COOLDOWN_MINUTES = 30

# ─── MARKET HOURS CHECK ──────────────────────────────────────────────────────

def is_market_open():
    now_utc = datetime.utcnow()
    # IST = UTC + 5:30
    ist_hour   = (now_utc.hour * 60 + now_utc.minute + 330) // 60 % 24
    ist_minute = (now_utc.minute + 30) % 60
    ist_total  = ist_hour * 60 + ist_minute
    market_open  = 9 * 60 + 15   # 09:15 IST
    market_close = 15 * 60 + 30  # 15:30 IST
    weekday = now_utc.weekday()   # 0=Mon … 4=Fri
    return weekday < 5 and market_open <= ist_total <= market_close


# ─── TELEGRAM ────────────────────────────────────────────────────────────────

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        print(f"[TELEGRAM] {msg[:80]}")
        return
    try:
        requests.post(
            f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
            json={'chat_id': TELEGRAM_CHAT, 'text': msg,
                  'parse_mode': 'Markdown', 'disable_web_page_preview': True},
            timeout=10
        )
    except Exception as e:
        print(f"  Telegram error: {e}")


# ─── ALERT STATE (cooldown tracker) ──────────────────────────────────────────

def load_alert_state():
    """Load today's alert state. Resets automatically on new day."""
    if not os.path.exists(ALERT_STATE_FILE):
        return {}
    try:
        with open(ALERT_STATE_FILE) as f:
            data = json.load(f)
        if data.get('date') != date.today().isoformat():
            return {}   # new day → reset
        return data.get('alerts', {})
    except Exception:
        return {}

def save_alert_state(alerts):
    with open(ALERT_STATE_FILE, 'w') as f:
        json.dump({'date': date.today().isoformat(), 'alerts': alerts}, f, indent=2)

def should_alert(alerts, sym, level_key):
    """Return True if cooldown has passed for sym+level."""
    key = f"{sym}_{level_key}"
    last = alerts.get(key)
    if not last:
        return True
    elapsed = (datetime.now() - datetime.fromisoformat(last)).total_seconds() / 60
    return elapsed >= COOLDOWN_MINUTES

def mark_alerted(alerts, sym, level_key):
    alerts[f"{sym}_{level_key}"] = datetime.now().isoformat()


# ─── FIB SCORING ─────────────────────────────────────────────────────────────

def score_fib(fib_levels, price, tl_dist_pct=None):
    """
    Weekly-only. Score 10 = price in {61.8%, 50%, 78.6%, 100%} pocket + TL ≤3%.
    """
    if not fib_levels:
        near_tl = tl_dist_pct is not None and tl_dist_pct <= 3.0
        return (7, 'TL Touch', 'tl') if near_tl else (0, '', '')

    p618_lo = fib_levels.get('pocket_618_low',  0); p618_hi = fib_levels.get('pocket_618_high', 0)
    p500_lo = fib_levels.get('pocket_500_low',  0); p500_hi = fib_levels.get('pocket_500_high', 0)
    p786_lo = fib_levels.get('pocket_786_low',  0); p786_hi = fib_levels.get('pocket_786_high', 0)
    p100_lo = fib_levels.get('pocket_100_low',  0); p100_hi = fib_levels.get('pocket_100_high', 0)

    in_618 = p618_lo > 0 and p618_lo <= price <= p618_hi
    in_500 = p500_lo > 0 and p500_lo <= price <= p500_hi
    in_786 = p786_lo > 0 and p786_lo <= price <= p786_hi
    in_100 = p100_lo > 0 and p100_lo <= price <= p100_hi
    near_tl = tl_dist_pct is not None and tl_dist_pct <= 3.0

    # Score 10: any of the 4 pockets + TL within 3%
    if near_tl and (in_618 or in_500 or in_786 or in_100):
        if in_618:   lvl = '61.8%'
        elif in_500: lvl = '50.0%'
        elif in_786: lvl = '78.6%'
        else:        lvl = '100%'
        return 10, f'Ultra: {lvl} Pocket + TL ({tl_dist_pct:.1f}%) ✓✓', 'ultra'

    if in_618: return 9,  f'61.8% Pocket (W:{fib_levels.get("61.8%_W",0):.0f})', '618'
    if in_500: return 8,  f'50.0% Pocket (W:{fib_levels.get("50.0%_W",0):.0f})', '500'
    if in_786: return 7,  f'78.6% Deep Pocket (W:{fib_levels.get("78.6%_W",0):.0f})', '786'
    if near_tl: return 7, f'TL Touch ({tl_dist_pct:.1f}%)', 'tl'
    if in_100: return 6,  f'100% Zone (W:{fib_levels.get("100.0%_W",0):.0f})', '100'
    return 0, '', ''


# ─── LIVE PRICE FETCH ─────────────────────────────────────────────────────────

def fetch_prices_yfinance(symbols):
    """Batch fetch latest prices via yfinance (no API key needed)."""
    prices = {}
    tickers = [s + '.NS' for s in symbols]

    # Split into batches of 50 to stay fast
    for i in range(0, len(tickers), 50):
        batch = tickers[i:i+50]
        try:
            data = yf.download(batch, period='1d', interval='1m',
                               auto_adjust=True, progress=False,
                               group_by='ticker', timeout=15)
            for t in batch:
                sym = t.replace('.NS', '')
                try:
                    if len(batch) == 1:
                        prices[sym] = float(data['Close'].iloc[-1])
                    else:
                        prices[sym] = float(data[t]['Close'].iloc[-1])
                except Exception:
                    pass
        except Exception as e:
            print(f"  yfinance batch error: {e}")
        time.sleep(0.5)

    return prices


def fetch_prices_angel(symbols):
    """
    Fetch live LTP from Angel One SmartAPI.
    Used as primary source if .env credentials are present.
    """
    prices = {}
    env_path = '.env'
    if not os.path.exists(env_path):
        return prices

    try:
        creds = {}
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and '=' in line and not line.startswith('#'):
                    k, v = line.split('=', 1)
                    creds[k.strip()] = v.strip()

        api_key    = creds.get('ANGEL_API_KEY', '')
        client_id  = creds.get('ANGEL_CLIENT_ID', '')
        password   = creds.get('ANGEL_PASSWORD', '')
        totp_secret= creds.get('ANGEL_TOTP_SECRET', '')

        if not all([api_key, client_id, password, totp_secret]):
            return prices

        from SmartApi import SmartConnect
        smart = SmartConnect(api_key=api_key)
        totp  = pyotp.TOTP(totp_secret).now()
        session = smart.generateSession(client_id, password, totp)

        if not (isinstance(session, dict) and session.get('status')):
            print("  Angel One session failed — falling back to yfinance")
            return prices

        print(f"  Angel One session OK ({client_id})")

        # Fetch LTP for each symbol
        for sym in symbols:
            try:
                ltp_data = smart.ltpData('NSE', sym + '-EQ', '')
                if ltp_data and ltp_data.get('status'):
                    ltp = float(ltp_data['data'].get('ltp', 0))
                    if ltp > 0:
                        prices[sym] = ltp
            except Exception:
                pass

    except ImportError:
        print("  SmartApi not installed — using yfinance")
    except Exception as e:
        print(f"  Angel One error: {e}")

    return prices


# ─── MAIN ────────────────────────────────────────────────────────────────────

def run():
    now_ist = datetime.utcnow()
    print(f"\n{'='*60}")
    print(f"  FIB PRICE ALERT — {now_ist.strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"{'='*60}")

    # Market hours gate
    if not is_market_open():
        print("  Market closed — skipping.")
        return

    # Load trendline cache
    if not os.path.exists(CACHE_FILE):
        print(f"  {CACHE_FILE} not found. Run --build first.")
        return

    with open(CACHE_FILE) as f:
        cache = json.load(f)
    trendlines = cache.get('trendlines', {})
    print(f"  Loaded {len(trendlines)} trendlines (built {cache.get('built_at','?')})")

    # Load today's alert state
    alerts = load_alert_state()

    # ── Fetch live prices ────────────────────────────────────────────────────
    symbols = list(trendlines.keys())
    print(f"\n  Fetching live prices for {len(symbols)} stocks...")

    # Try Angel One first (real-time), fall back to yfinance (delayed ~15 min)
    prices = fetch_prices_angel(symbols)
    yf_needed = [s for s in symbols if s not in prices]

    if yf_needed:
        print(f"  Fetching {len(yf_needed)} prices via yfinance...")
        yf_prices = fetch_prices_yfinance(yf_needed)
        prices.update(yf_prices)

    print(f"  Got {len(prices)} prices")

    # ── Score each stock ─────────────────────────────────────────────────────
    critical_alerts = []
    high_alerts     = []
    medium_alerts   = []
    tl_touch_alerts = []

    for sym, tl in trendlines.items():
        price = prices.get(sym)
        if not price:
            continue

        fib = tl.get('fib_levels', {})

        # ── Fib pocket check — pass trendline distance for ULTRA scoring
        score, label, pocket = score_fib(fib, price, tl_dist if tl_dist < 99 else None)

        # ── Trendline proximity check
        last_date = tl.get('last_date', '')
        tl_price = 0
        if last_date:
            from datetime import datetime as dt
            last_dt = dt.strptime(last_date, '%Y-%m-%d')
            today   = dt.now()
            months  = (today.year - last_dt.year) * 12 + (today.month - last_dt.month)
            tl_price = round(tl['slope'] * (tl['last_idx'] + months) + tl['intercept'], 2)

        tl_dist = abs((price - tl_price) / tl_price * 100) if tl_price > 0 else 99
        near_tl = tl_dist <= TL_TOUCH_PCT

        # ── Determine alert tier
        target_1 = fib.get('Ext_23.6%_W')
        target_2 = fib.get('Ext_61.8%_W')
        sl_price = round(price * 0.92, 2)  # 8% SL from current

        entry_info = (sym, price, tl_price, tl_dist, score, label,
                      target_1, target_2, sl_price, tl['n_touches'])

        if score == SCORE_CRITICAL and should_alert(alerts, sym, 'ultra'):
            critical_alerts.append(entry_info)

        elif score == SCORE_HIGH and should_alert(alerts, sym, '618'):
            high_alerts.append(entry_info)

        elif score == SCORE_MEDIUM and should_alert(alerts, sym, '500'):
            medium_alerts.append(entry_info)

        if near_tl and score < SCORE_MEDIUM and should_alert(alerts, sym, 'tl'):
            tl_touch_alerts.append(entry_info)

    # ── Send alerts ──────────────────────────────────────────────────────────
    total_sent = 0

    def fmt_target(p):
        return f'₹{p:,.2f}' if p else '—'

    for info in critical_alerts:
        sym, price, tl_price, tl_dist, score, label, t1, t2, sl, touches = info
        msg = (
            f"🔥 *ULTRA-CONFLUENCE — {sym}* 🔥\n\n"
            f"📍 CMP: ₹{price:,.2f}\n"
            f"🎯 Zone: {label}\n"
            f"📐 Trendline: ₹{tl_price:,.2f} ({tl_dist:+.1f}%) | {touches} touches\n"
            f"🛑 SL: {fmt_target(sl)}\n"
            f"✅ T1: {fmt_target(t1)}\n"
            f"✅ T2: {fmt_target(t2)}\n"
            f"⭐ Score: {score}/10\n\n"
            f"[Open Chart](https://in.tradingview.com/chart/?symbol=NSE:{sym}) | "
            f"[App]({APP_URL}/)"
        )
        send_telegram(msg)
        mark_alerted(alerts, sym, 'ultra')
        total_sent += 1
        print(f"  🔥 CRITICAL: {sym} ₹{price:.2f} — {label[:40]}")

    for info in high_alerts:
        sym, price, tl_price, tl_dist, score, label, t1, t2, sl, touches = info
        msg = (
            f"⚡ *61.8% GOLDEN POCKET — {sym}*\n\n"
            f"📍 CMP: ₹{price:,.2f}\n"
            f"🎯 Zone: {label}\n"
            f"📐 Trendline: ₹{tl_price:,.2f} ({tl_dist:+.1f}%) | {touches} touches\n"
            f"🛑 SL: {fmt_target(sl)}\n"
            f"✅ T1: {fmt_target(t1)}\n"
            f"✅ T2: {fmt_target(t2)}\n"
            f"⭐ Score: {score}/10\n\n"
            f"[Open Chart](https://in.tradingview.com/chart/?symbol=NSE:{sym}) | "
            f"[App]({APP_URL}/)"
        )
        send_telegram(msg)
        mark_alerted(alerts, sym, '618')
        total_sent += 1
        print(f"  ⚡ HIGH:     {sym} ₹{price:.2f} — {label[:40]}")

    for info in medium_alerts:
        sym, price, tl_price, tl_dist, score, label, t1, t2, sl, touches = info
        msg = (
            f"📊 *50% POCKET ENTRY — {sym}*\n\n"
            f"📍 CMP: ₹{price:,.2f}\n"
            f"🎯 Zone: {label}\n"
            f"📐 Trendline: ₹{tl_price:,.2f} ({tl_dist:+.1f}%) | {touches} touches\n"
            f"🛑 SL: {fmt_target(sl)}\n"
            f"✅ T1: {fmt_target(t1)}\n"
            f"✅ T2: {fmt_target(t2)}\n"
            f"⭐ Score: {score}/10\n\n"
            f"[Open Chart](https://in.tradingview.com/chart/?symbol=NSE:{sym}) | "
            f"[App]({APP_URL}/)"
        )
        send_telegram(msg)
        mark_alerted(alerts, sym, '500')
        total_sent += 1
        print(f"  📊 MEDIUM:  {sym} ₹{price:.2f} — {label[:40]}")

    for info in tl_touch_alerts:
        sym, price, tl_price, tl_dist, score, label, t1, t2, sl, touches = info
        msg = (
            f"📈 *TRENDLINE TOUCH — {sym}*\n\n"
            f"📍 CMP: ₹{price:,.2f}\n"
            f"📐 Trendline: ₹{tl_price:,.2f} ({tl_dist:+.1f}%) | {touches} touches\n"
            f"🛑 SL: {fmt_target(sl)}\n"
            f"✅ T1: {fmt_target(t1)}\n"
            f"⭐ Score: {score}/10 (no fib pocket)\n\n"
            f"[Open Chart](https://in.tradingview.com/chart/?symbol=NSE:{sym}) | "
            f"[App]({APP_URL}/)"
        )
        send_telegram(msg)
        mark_alerted(alerts, sym, 'tl')
        total_sent += 1
        print(f"  📈 TL:      {sym} ₹{price:.2f} — dist {tl_dist:.1f}%")

    # Save updated alert state
    save_alert_state(alerts)

    # ── Summary ──────────────────────────────────────────────────────────────
    print(f"\n  Alerts sent: {total_sent}")
    print(f"    🔥 Ultra-Confluence (10/10): {len(critical_alerts)}")
    print(f"    ⚡ 61.8% Pocket    ( 9/10): {len(high_alerts)}")
    print(f"    📊 50% Pocket      ( 8/10): {len(medium_alerts)}")
    print(f"    📈 TL Touch only:           {len(tl_touch_alerts)}")

    if total_sent == 0:
        print("  — No new alerts (all in cooldown or no stocks in zone)")

    print(f"{'='*60}\n")


if __name__ == '__main__':
    run()
