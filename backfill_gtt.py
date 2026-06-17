#!/usr/bin/env python3
"""
Backfill GTT for existing open trades that don't have a GTT yet.
Run once on EC2: python3 backfill_gtt.py

Reads radar_trades.json, finds open trades with no gtt_id,
places GTT OCO (target + SL) for each, saves gtt_id back.
"""
import os, json, pyotp
from datetime import datetime
from SmartApi import SmartConnect
import requests

RADAR_FILE = 'radar_trades.json'
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT  = os.environ.get('TELEGRAM_CHAT_ID', '')

# Read from .env if env vars empty
if not TELEGRAM_TOKEN and os.path.exists('.env'):
    with open('.env') as f:
        for line in f:
            line = line.strip()
            if '=' in line and not line.startswith('#'):
                k, v = line.split('=', 1)
                if k.strip() == 'TELEGRAM_BOT_TOKEN': TELEGRAM_TOKEN = v.strip()
                if k.strip() == 'TELEGRAM_CHAT_ID':   TELEGRAM_CHAT  = v.strip()

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT: print(f"[TG] {msg[:80]}"); return
    try:
        requests.post(f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
            json={'chat_id': TELEGRAM_CHAT, 'text': msg, 'parse_mode': 'Markdown'}, timeout=8)
    except: pass

def get_session():
    creds = {}
    if os.path.exists('.env'):
        with open('.env') as f:
            for line in f:
                line = line.strip()
                if '=' in line and not line.startswith('#'):
                    k, v = line.split('=', 1)
                    creds[k.strip()] = v.strip()
    smart = SmartConnect(api_key=creds.get('ANGEL_API_KEY', ''))
    totp  = pyotp.TOTP(creds.get('ANGEL_TOTP_SECRET', '')).now()
    sess  = smart.generateSession(creds.get('ANGEL_CLIENT_ID', ''), creds.get('ANGEL_PASSWORD', ''), totp)
    if not (isinstance(sess, dict) and sess.get('status')):
        raise Exception(f"Angel One session failed: {sess}")
    print(f"  ✅ Angel One session OK ({creds.get('ANGEL_CLIENT_ID')})")
    return smart

def get_symbol_token(smart, symbol):
    """Get NSE symbol token for a stock."""
    result = smart.searchScrip('NSE', symbol)
    if not result or not result.get('data'): return None, None
    for r in result['data']:
        if r.get('tradingsymbol') == symbol + '-EQ':
            return r['tradingsymbol'], r['symboltoken']
    # fallback to first result
    r = result['data'][0]
    return r['tradingsymbol'], r['symboltoken']

def place_gtt(smart, trading_symbol, symbol_token, quantity, entry_price, target_price, stop_loss):
    """Place GTT OCO order using correct Angel One API format."""
    gtt_params = {
        "tradingsymbol":        trading_symbol,
        "symboltoken":          symbol_token,
        "exchange":             "NSE",
        "producttype":          "DELIVERY",
        "transactiontype":      "SELL",
        "qty":                  str(quantity),
        "disclosedqty":         str(quantity),
        "price":                round(target_price * 0.995, 2),  # limit price for target leg
        "triggerprice":         round(target_price, 2),          # target trigger
        "timeperiod":           365,
        "gttType":              "OCO",
        "stoplossprice":        round(stop_loss * 0.995, 2),     # SL limit price
        "stoplosstriggerprice": round(stop_loss, 2),             # SL trigger price
    }
    result = smart.gttCreateRule(gtt_params)
    return str(result) if result else None

def main():
    print(f"\n{'='*60}")
    print(f"  GTT BACKFILL — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}")

    if not os.path.exists(RADAR_FILE):
        print("  radar_trades.json not found"); return

    with open(RADAR_FILE) as f:
        trades = json.load(f)

    # Find open trades with no GTT
    open_trades = [t for t in trades
                   if t.get('status') in ('Open', 'Triggered', 'open', 'triggered')
                   and not t.get('gtt_id')]

    if not open_trades:
        print("  All open trades already have GTT or no open trades found")
        return

    print(f"  Found {len(open_trades)} open trades without GTT:")
    for t in open_trades:
        print(f"    {t.get('ticker')} | entry={t.get('entry_price')} target={t.get('target')} sl={t.get('stop_loss')} qty={t.get('quantity')}")

    # Connect to Angel One
    try:
        smart = get_session()
    except Exception as e:
        print(f"  ❌ Cannot connect to Angel One: {e}"); return

    changed = False
    for trade in trades:
        ticker = trade.get('ticker', '')
        status = trade.get('status', '')
        gtt_id = trade.get('gtt_id')

        if status not in ('Open', 'Triggered', 'open', 'triggered') or gtt_id:
            continue

        entry_price  = float(trade.get('entry_price', 0) or 0)
        target_price = float(trade.get('target', 0) or 0)
        stop_loss    = float(trade.get('stop_loss', 0) or 0)
        quantity     = int(trade.get('quantity', 0) or 0)

        if not ticker or entry_price <= 0 or target_price <= 0 or stop_loss <= 0 or quantity <= 0:
            print(f"  ⚠️  {ticker}: missing data (entry={entry_price} target={target_price} sl={stop_loss} qty={quantity}) — skipping")
            continue

        print(f"\n  Processing {ticker}...")

        # Get symbol token
        try:
            trading_symbol, symbol_token = get_symbol_token(smart, ticker)
            if not trading_symbol:
                print(f"  ❌ {ticker}: symbol not found on Angel One"); continue
            print(f"  Symbol: {trading_symbol} | Token: {symbol_token}")
        except Exception as e:
            print(f"  ❌ {ticker}: symbol search failed: {e}"); continue

        # Place GTT
        try:
            new_gtt_id = place_gtt(smart, trading_symbol, symbol_token,
                                   quantity, entry_price, target_price, stop_loss)
            if new_gtt_id:
                trade['gtt_id']     = new_gtt_id
                trade['gtt_status'] = 'ACTIVE'
                changed = True
                print(f"  ✅ GTT placed: {new_gtt_id}")
                pnl_t = round((target_price - entry_price) / entry_price * 100, 1)
                pnl_s = round((stop_loss - entry_price) / entry_price * 100, 1)
                msg = (
                    f"✅ *GTT BACKFILLED — {ticker}*\n\n"
                    f"Entry: ₹{entry_price:,.2f} × {quantity}\n"
                    f"Target: ₹{target_price:,.2f} ({pnl_t:+.1f}%)\n"
                    f"SL: ₹{stop_loss:,.2f} ({pnl_s:+.1f}%)\n"
                    f"GTT ID: {new_gtt_id}"
                )
                send_telegram(msg)
            else:
                print(f"  ❌ {ticker}: GTT returned empty")
        except Exception as e:
            print(f"  ❌ {ticker}: GTT failed: {e}")

    if changed:
        with open(RADAR_FILE, 'w') as f:
            json.dump(trades, f, indent=2)
        print(f"\n  ✅ radar_trades.json updated with GTT IDs")
    else:
        print(f"\n  No changes made")

    print(f"{'='*60}\n")

if __name__ == '__main__':
    main()
