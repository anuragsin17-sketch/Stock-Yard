#!/usr/bin/env python3
"""
Nifty IT Index Monitor
Tracks Nifty IT index and sends Telegram alert when it drops 500+ points from day's high.
"""

import os
import json
import time
import requests
from datetime import datetime

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT  = os.environ.get('TELEGRAM_CHAT_ID', '')
STATE_FILE     = '/home/ubuntu/nifty_it_state.json'
CHECK_INTERVAL = 300  # 5 minutes

def send_telegram(message):
    """Send Telegram notification"""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        print("⚠️ Telegram not configured")
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        resp = requests.post(url, json={
            'chat_id': TELEGRAM_CHAT,
            'text': message,
            'parse_mode': 'Markdown'
        }, timeout=10)
        if resp.status_code == 200:
            print("✅ Telegram sent")
            return True
        print(f"❌ Telegram failed: {resp.status_code}")
    except Exception as e:
        print(f"❌ Telegram error: {e}")
    return False


def get_nifty_it_price():
    """Fetch Nifty IT index price from NSE"""
    try:
        # Nifty IT symbol on NSE
        api_url = 'https://32-194-58-75.nip.io/api/get-quote?symbol=NIFTYIT'
        resp = requests.get(api_url, timeout=10, verify=False)
        if resp.status_code == 200:
            data = resp.json()
            if data.get('success'):
                ltp = float(data.get('ltp', 0))
                if ltp > 0:
                    return ltp
        print(f"❌ Nifty IT fetch failed: {resp.status_code}")
    except Exception as e:
        print(f"❌ Nifty IT fetch error: {e}")
    return None


def load_state():
    """Load tracking state"""
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    """Save tracking state"""
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"❌ State save error: {e}")


def check_nifty_it():
    """Check Nifty IT and send alert if dropped 500+ points"""
    current_price = get_nifty_it_price()
    if not current_price:
        return

    state = load_state()
    today = datetime.now().strftime('%Y-%m-%d')
    
    # Reset state daily
    if state.get('date') != today:
        state = {
            'date': today,
            'day_high': current_price,
            'alerted_500': False
        }
    
    # Track day's high
    if current_price > state.get('day_high', 0):
        state['day_high'] = current_price
        print(f"📊 Nifty IT new high: ₹{current_price:,.2f}")
    
    # Calculate drop from day's high
    day_high = state.get('day_high', current_price)
    drop = day_high - current_price
    drop_pct = (drop / day_high * 100) if day_high > 0 else 0
    
    print(f"📊 Nifty IT: ₹{current_price:,.2f} | High: ₹{day_high:,.2f} | Drop: {drop:,.2f} ({drop_pct:.2f}%)")
    
    # Alert if dropped 500+ points and not already alerted today
    if drop >= 500 and not state.get('alerted_500'):
        send_telegram(
            f"🚨 *NIFTY IT ALERT - 500 POINT DROP*\n\n"
            f"Current: ₹{current_price:,.2f}\n"
            f"Day High: ₹{day_high:,.2f}\n"
            f"Drop: *{drop:,.2f} points* ({drop_pct:.2f}%)\n\n"
            f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M IST')}"
        )
        state['alerted_500'] = True
        print(f"🚨 ALERT SENT: Nifty IT dropped {drop:,.2f} points")
    
    save_state(state)


def main():
    print("=" * 60)
    print("NIFTY IT MONITOR STARTED")
    print(f"Checking every {CHECK_INTERVAL}s for 500-point drops")
    print("=" * 60)
    
    send_telegram("🟢 *Nifty IT Monitor started* — tracking 500-point drops")
    
    while True:
        try:
            check_nifty_it()
        except Exception as e:
            print(f"❌ Monitor loop error: {e}")
        time.sleep(CHECK_INTERVAL)


if __name__ == '__main__':
    main()
