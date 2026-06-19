#!/usr/bin/env python3
"""
Live Price Monitor — Every 1 Minute (EC2 Cron)
===============================================
Tracks ALL stocks from:
  1. Trendline tab  (trendline_cache.json  — checks if price near trendline)
  2. Volume tab     (volume_gainer_watchlist.json — checks if price near prev day low)

Sends Telegram when:
  - Trendline: price within ±1% of trendline trigger
  - Volume:    price within ±1% of prev_day_low (entry zone)

Cooldown: 30 min per stock per alert type (no spam)

EC2 crontab setup (run once):
  * 9-15 * * 1-5 cd /home/ubuntu/Stock-Yard && python live_price_monitor.py >> logs/live_monitor.log 2>&1

Dependencies (already installed on EC2):
  pip install yfinance requests pyotp SmartApi-python pandas
"""

import os, json, time, requests
from datetime import datetime, date
import yfinance as yf
import pandas as pd
import warnings
warnings.filterwarnings('ignore')

# ─── CONFIG ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT    = os.environ.get('TELEGRAM_CHAT_ID', '')

# Also try .env file if env vars not set
if not TELEGRAM_TOKEN and os.path.exists('.env'):
    try:
        with open('.env') as _f:
            for _line in _f:
                _line = _line.strip()
                if '=' in _line and not _line.startswith('#'):
                    _k, _v = _line.split('=', 1)
                    if _k.strip() == 'TELEGRAM_BOT_TOKEN': TELEGRAM_TOKEN = _v.strip()
                    if _k.strip() == 'TELEGRAM_CHAT_ID':   TELEGRAM_CHAT  = _v.strip()
    except: pass
APP_URL          = 'https://anuragsin17-sketch.github.io/Stock-Yard-Public'
TL_CACHE_FILE    = 'trendline_cache.json'
VOL_WATCH_FILE   = 'volume_gainer_watchlist.json'
STATE_FILE       = 'alerted_1min.json'
COOLDOWN_MINUTES = 30        # don't re-alert same stock+type for 30 min (kept for reference)
TL_ALERT_PCT     = 1.0       # trendline: ±1% of trendline price
VOL_ALERT_PCT    = 1.0       # volume:    ±1% of prev day low

# ─── MARKET HOURS ────────────────────────────────────────────────────────────

def is_market_open():
    now = datetime.utcnow()
    ist_min = (now.hour * 60 + now.minute + 330) % (24 * 60)
    market_open  = 9 * 60 + 15
    market_close = 15 * 60 + 30
    return now.weekday() < 5 and market_open <= ist_min <= market_close

# ─── TELEGRAM ────────────────────────────────────────────────────────────────

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        print(f"[TG] {msg[:80]}")
        return
    try:
        requests.post(
            f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
            json={'chat_id': TELEGRAM_CHAT, 'text': msg,
                  'parse_mode': 'Markdown', 'disable_web_page_preview': True},
            timeout=8
        )
    except Exception as e:
        print(f"  Telegram error: {e}")

# ─── ALERT STATE (cooldown) ───────────────────────────────────────────────────

def load_state():
    if not os.path.exists(STATE_FILE): return {}
    try:
        with open(STATE_FILE) as f:
            d = json.load(f)
        if d.get('date') != date.today().isoformat():
            return {}
        return d.get('alerts', {})
    except: return {}

def save_state(alerts):
    with open(STATE_FILE, 'w') as f:
        json.dump({'date': date.today().isoformat(), 'alerts': alerts}, f)

def can_alert(alerts, sym, alert_type):
    key = f"{sym}_{alert_type}"
    last = alerts.get(key)
    if not last: return True
    # Once per day — if already alerted today, skip
    try:
        last_date = datetime.fromisoformat(last).date()
        return last_date != date.today()
    except:
        return True

def mark_alerted(alerts, sym, alert_type):
    alerts[f"{sym}_{alert_type}"] = datetime.now().isoformat()

# ─── PRICE FETCH ─────────────────────────────────────────────────────────────

def fetch_prices(symbols):
    """Batch fetch latest prices via yfinance. Returns {sym: (close, low)}"""
    prices = {}
    tickers = [s + '.NS' for s in symbols]
    batch_size = 80
    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i+batch_size]
        try:
            data = yf.download(batch, period='2d', interval='1d',
                               auto_adjust=True, progress=False,
                               group_by='ticker', timeout=20)
            for t in batch:
                sym = t.replace('.NS', '')
                try:
                    if len(batch) == 1:
                        df = data[['Close','Low']].dropna()
                    else:
                        df = data[t][['Close','Low']].dropna()
                    if df.empty: continue
                    prices[sym] = (float(df['Close'].iloc[-1]), float(df['Low'].iloc[-1]))
                except: pass
        except Exception as e:
            print(f"  Batch error: {e}")
        time.sleep(0.3)
    return prices

# Try Angel One for real-time prices (fallback to yfinance)
def fetch_prices_angel(symbols):
    prices = {}
    env_path = '.env'
    if not os.path.exists(env_path): return prices
    try:
        creds = {}
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and '=' in line and not line.startswith('#'):
                    k, v = line.split('=', 1)
                    creds[k.strip()] = v.strip()
        import pyotp
        from SmartApi import SmartConnect
        smart = SmartConnect(api_key=creds.get('ANGEL_API_KEY',''))
        totp  = pyotp.TOTP(creds.get('ANGEL_TOTP_SECRET','')).now()
        sess  = smart.generateSession(creds.get('ANGEL_CLIENT_ID',''), creds.get('ANGEL_PASSWORD',''), totp)
        if not (isinstance(sess, dict) and sess.get('status')): return prices
        for sym in symbols:
            try:
                ltp_data = smart.ltpData('NSE', sym+'-EQ', '')
                if ltp_data and ltp_data.get('status'):
                    ltp = float(ltp_data['data'].get('ltp', 0))
                    if ltp > 0: prices[sym] = (ltp, ltp)  # use LTP for both close & low
            except: pass
    except ImportError: pass
    except Exception as e: print(f"  Angel One error: {e}")
    return prices

# ─── TRENDLINE PRICE ─────────────────────────────────────────────────────────

def get_tl_price(tl):
    try:
        last_date = datetime.strptime(tl['last_date'], '%Y-%m-%d')
        today     = datetime.now()
        months    = (today.year - last_date.year)*12 + (today.month - last_date.month)
        return round(tl['slope']*(tl['last_idx']+months)+tl['intercept'], 2)
    except: return 0

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def run():
    now_str = datetime.now().strftime('%H:%M:%S')
    print(f"\n[{now_str}] Live Price Monitor", flush=True)

    if not is_market_open():
        print("  Market closed — skip")
        return

    alerts = load_state()

    # ── Load stock lists ──────────────────────────────────────────────────────
    tl_stocks  = {}  # sym → tl_price
    vol_stocks = {}  # sym → prev_day_low

    if os.path.exists(TL_CACHE_FILE):
        try:
            with open(TL_CACHE_FILE) as f:
                cache = json.load(f)
            for sym, tl in cache.get('trendlines', {}).items():
                tp = get_tl_price(tl)
                if tp > 0: tl_stocks[sym] = tp
        except: pass

    if os.path.exists(VOL_WATCH_FILE):
        try:
            with open(VOL_WATCH_FILE) as f:
                vol_list = json.load(f)
            for s in (vol_list if isinstance(vol_list, list) else []):
                sym = s.get('ticker','')
                low = float(s.get('prev_day_low', 0) or 0)
                if sym and low > 0: vol_stocks[sym] = low
        except: pass

    all_syms = list(set(list(tl_stocks.keys()) + list(vol_stocks.keys())))
    if not all_syms:
        print("  No stocks to monitor"); return

    print(f"  Monitoring {len(tl_stocks)} trendline + {len(vol_stocks)} volume stocks")

    # ── Fetch live prices ─────────────────────────────────────────────────────
    print("  Fetching prices (Angel One first, yfinance fallback)...")
    prices = fetch_prices_angel(all_syms)
    missing = [s for s in all_syms if s not in prices]
    if missing:
        yf_prices = fetch_prices(missing)
        prices.update(yf_prices)
    print(f"  Got {len(prices)} prices")

    # ── Check trendline stocks ────────────────────────────────────────────────
    tl_alerts = 0
    for sym, tl_price in tl_stocks.items():
        if sym not in prices: continue
        ltp, low = prices[sym]
        dist_pct = abs((ltp - tl_price) / tl_price * 100)
        if dist_pct <= TL_ALERT_PCT and can_alert(alerts, sym, 'tl'):
            chart = f"https://in.tradingview.com/chart/?symbol=NSE:{sym}"
            msg = (
                f"📥 *ENTRY PRICE TRIGGERED — {sym}*\n\n"
                f"📌 Tab: *Trendline*\n"
                f"💰 Live Price: ₹{ltp:,.2f}\n"
                f"📍 Entry Trigger: ₹{tl_price:,.2f}\n"
                f"📏 Distance: {dist_pct:.2f}% (within ±{TL_ALERT_PCT}%)\n"
                f"⏰ {datetime.now().strftime('%d %b %Y %H:%M IST')}\n\n"
                f"[View Chart]({chart}) | [Open App]({APP_URL}/)"
            )
            send_telegram(msg)
            mark_alerted(alerts, sym, 'tl')
            tl_alerts += 1
            print(f"  📥 TL entry triggered: {sym} @ ₹{ltp:.2f} | trigger ₹{tl_price:.2f} | dist {dist_pct:.2f}%")

    # ── Check volume stocks ───────────────────────────────────────────────────
    vol_alerts = 0
    for sym, prev_low in vol_stocks.items():
        if sym not in prices: continue
        ltp, low = prices[sym]
        dist_pct = abs((ltp - prev_low) / prev_low * 100)
        if dist_pct <= VOL_ALERT_PCT and can_alert(alerts, sym, 'vol'):
            chart = f"https://in.tradingview.com/chart/?symbol=NSE:{sym}"
            msg = (
                f"📥 *ENTRY PRICE TRIGGERED — {sym}*\n\n"
                f"📌 Tab: *Volume*\n"
                f"💰 Live Price: ₹{ltp:,.2f}\n"
                f"📍 Entry Zone (Prev Low): ₹{prev_low:,.2f}\n"
                f"📏 Distance: {dist_pct:.2f}% (within ±{VOL_ALERT_PCT}%)\n"
                f"⏰ {datetime.now().strftime('%d %b %Y %H:%M IST')}\n\n"
                f"[View Chart]({chart}) | [Open App]({APP_URL}/)"
            )
            send_telegram(msg)
            mark_alerted(alerts, sym, 'vol')
            vol_alerts += 1
            print(f"  📥 Vol entry triggered: {sym} @ ₹{ltp:.2f} | entry ₹{prev_low:.2f} | dist {dist_pct:.2f}%")

    save_state(alerts)
    print(f"  Done — TL alerts: {tl_alerts} | Vol alerts: {vol_alerts}")

if __name__ == '__main__':
    run()
