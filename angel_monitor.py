#!/usr/bin/env python3
"""
Stock Yard Trade Monitor
- Runs every 15 min during market hours
- Checks Volume & Trendline stocks for entry price hits
- Automatically moves triggered stocks to Radar tab
- Monitors Radar stocks for target/stoploss hits
- Sends Telegram notifications on all triggers
"""

import os
import json
import requests
from datetime import datetime

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT = os.environ.get('TELEGRAM_CHAT_ID')
DATA_FILE = 'data.json'
TRENDLINE_FILE = 'trendline_screen.json'
RADAR_FILE = 'radar_trades.json'


def send_telegram(message: str) -> bool:
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
        print(f"❌ Telegram failed: {resp.text[:200]}")
    except Exception as e:
        print(f"❌ Telegram error: {e}")
    return False


def send_telegram_with_action(ticker: str, entry_price: float, current_price: float, 
                              target_price: float, stoploss_price: float, source: str) -> bool:
    """Send Telegram with Confirm/Skip inline buttons"""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        print("⚠️ Telegram not configured")
        return False
    
    try:
        BASE_URL = "https://anuragsin17-sketch.github.io/Stock-Yard-Public"
        qty = max(1, int(50000 / entry_price))
        
        message = (
            f"🎯 *TRADE TRIGGERED - {source.upper()}*\n\n"
            f"Stock: *{ticker}*\n"
            f"Entry: ₹{entry_price:,.2f}\n"
            f"Current: ₹{current_price:,.2f}\n"
            f"Target: ₹{target_price:,.2f} _(+20%)_\n"
            f"Stop Loss: ₹{stoploss_price:,.2f}\n"
            f"Qty: {qty} shares\n"
            f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M IST')}"
        )
        
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        resp = requests.post(url, json={
            'chat_id': TELEGRAM_CHAT,
            'text': message,
            'parse_mode': 'Markdown'
        }, timeout=10)
        
        if resp.status_code == 200:
            print("✅ Telegram with buttons sent")
            return True
        print(f"❌ Telegram failed: {resp.text[:200]}")
    except Exception as e:
        print(f"❌ Telegram error: {e}")
    return False


def get_live_price(ticker: str) -> float:
    """Get current price from Angel One API"""
    try:
        symbol = ticker.replace('.NS', '')
        # Use HTTPS nip.io domain — reachable from GitHub Actions
        api_url = f"https://32-194-58-75.nip.io/api/get-quote?symbol={symbol}"
        response = requests.get(api_url, timeout=8, verify=False)
        if response.status_code == 200:
            data = response.json()
            if data.get('success'):
                ltp = float(data.get('ltp', 0))
                if ltp > 0:
                    return ltp
        print(f"Price fetch error for {ticker}: API returned {response.status_code}")
    except Exception as e:
        print(f"Price fetch error for {ticker}: {e}")
    return None


def load_json(filepath: str) -> dict:
    """Load JSON file"""
    if not os.path.exists(filepath):
        return {}
    try:
        with open(filepath, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading {filepath}: {e}")
        return {}


def save_json(filepath: str, data):
    """Save JSON file"""
    try:
        with open(filepath, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"Error saving {filepath}: {e}")


def load_radar() -> list:
    """Load radar trades"""
    data = load_json(RADAR_FILE)
    return data if isinstance(data, list) else []


def save_radar(trades: list):
    """Save radar trades"""
    save_json(RADAR_FILE, trades)


def check_gtt_exits():
    """
    Check all active GTT orders on Angel One.
    If triggered (target or SL hit) → close trade in radar + send Telegram.
    If buy order expired/cancelled → cancel GTT to avoid accidental short sell.
    """
    print("\n🎯 Checking GTT exit orders...")

    radar_trades = load_radar()
    if not radar_trades:
        print("  No trades in Radar")
        return False

    # Only trades that have a GTT id
    gtt_trades = [t for t in radar_trades if t.get('gtt_id') and t.get('gtt_status') == 'ACTIVE']
    if not gtt_trades:
        print("  No active GTT orders to check")
        return False

    print(f"  Checking {len(gtt_trades)} active GTT orders...")

    # Connect to Angel One
    try:
        creds = {}
        if os.path.exists('.env'):
            with open('.env') as f:
                for line in f:
                    line = line.strip()
                    if line and '=' in line and not line.startswith('#'):
                        k, v = line.split('=', 1)
                        creds[k.strip()] = v.strip()

        from SmartApi import SmartConnect
        import pyotp
        smart = SmartConnect(api_key=creds.get('ANGEL_API_KEY', ''))
        totp  = pyotp.TOTP(creds.get('ANGEL_TOTP_SECRET', '')).now()
        session = smart.generateSession(
            creds.get('ANGEL_CLIENT_ID', ''),
            creds.get('ANGEL_PASSWORD', ''),
            totp
        )
        if not (isinstance(session, dict) and session.get('status')):
            print("  Angel One session failed for GTT check")
            return False
    except Exception as e:
        print(f"  Cannot connect to Angel One: {e}")
        return False

    changed = False

    for trade in radar_trades:
        gtt_id = trade.get('gtt_id')
        if not gtt_id or trade.get('gtt_status') != 'ACTIVE':
            continue

        ticker      = trade.get('ticker', '')
        entry_price = float(trade.get('entry_price', 0))
        target      = float(trade.get('target', 0))
        stop_loss   = float(trade.get('stop_loss', 0))
        quantity    = int(trade.get('quantity', 0))

        try:
            # Fetch GTT rule status
            gtt_result = smart.gttDetails(gtt_id)
            if not (isinstance(gtt_result, dict) and gtt_result.get('status')):
                continue

            gtt_data   = gtt_result.get('data', {})
            gtt_status = gtt_data.get('status', '').upper()  # ACTIVE / TRIGGERED / CANCELLED / EXPIRED

            if gtt_status in ('TRIGGERED', 'FORALL'):
                # Determine which leg triggered (target or SL)
                exit_price   = float(gtt_data.get('triggerprice', [{}])[0].get('price', 0) or 0)
                triggered_leg = gtt_data.get('triggerprice', [])

                # Find which leg fired — compare triggered price to target vs SL
                if exit_price >= target * 0.99:
                    exit_reason = 'Target Hit'
                    icon = '🎯'
                    exit_price  = target
                else:
                    exit_reason = 'Stop Loss Hit'
                    icon = '🛑'
                    exit_price  = stop_loss

                pnl_pct = round((exit_price - entry_price) / entry_price * 100, 2)

                # Update trade record
                trade['status']     = 'Closed'
                trade['exit_price'] = round(exit_price, 2)
                trade['closed_at']  = datetime.now().isoformat()
                trade['pnl_pct']    = pnl_pct
                trade['exit_reason']= exit_reason
                trade['gtt_status'] = 'TRIGGERED'
                changed = True

                # Telegram exit notification
                msg = (
                    f"{icon} *POSITION CLOSED — {exit_reason}*\n\n"
                    f"Stock: *{ticker}*\n"
                    f"Entry: ₹{entry_price:,.2f}\n"
                    f"Exit:  ₹{exit_price:,.2f}\n"
                    f"P&L:   *{pnl_pct:+.2f}%*\n"
                    f"Qty:   {quantity} shares\n"
                    f"GTT:   {gtt_id}\n"
                    f"Time:  {datetime.now().strftime('%Y-%m-%d %H:%M IST')}"
                )
                send_telegram(msg)
                print(f"  {icon} GTT triggered: {ticker} | {exit_reason} | P&L {pnl_pct:+.2f}%")

            elif gtt_status in ('CANCELLED', 'EXPIRED', 'REJECTED'):
                trade['gtt_status'] = gtt_status
                changed = True
                msg = (
                    f"⚠️ *GTT {gtt_status} — {ticker}*\n\n"
                    f"GTT ID: {gtt_id}\n"
                    f"Entry: ₹{entry_price:,.2f}\n"
                    f"Target: ₹{target:,.2f} | SL: ₹{stop_loss:,.2f}\n"
                    f"Action needed: Re-place GTT manually on Angel One\n"
                    f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M IST')}"
                )
                send_telegram(msg)
                print(f"  ⚠️ GTT {gtt_status}: {ticker}")

        except Exception as e:
            print(f"  GTT check error for {ticker}: {e}")

    if changed:
        save_radar(radar_trades)
    return changed


def check_trendline_stocks_for_entry():
    """Check Trendline stocks for entry price hits"""
    print("\n📈 Checking Trendline stocks...")
    trendline_data = load_json(TRENDLINE_FILE)
    if not isinstance(trendline_data, list):
        trendline_data = []
    
    radar_trades = load_radar()
    changed = False
    
    # Load stocks already in dashboard tabs
    main_data = load_json(DATA_FILE)
    volume_stocks = main_data.get('volume_breakout_stocks', []) if isinstance(main_data, dict) else []
    golden_stocks = main_data.get('golden_stocks', []) if isinstance(main_data, dict) else []
    
    # Load orders already placed (prevent duplicate orders)
    orders_data = load_json('angel_orders.json')
    ordered_stocks = set()
    if isinstance(orders_data, list):
        ordered_stocks = {o.get('symbol', '') for o in orders_data if o.get('symbol')}
    print(f"  Already ordered: {len(ordered_stocks)} stocks - {', '.join(sorted(ordered_stocks)[:5])}")

    for stock in trendline_data:
        ticker = stock.get('ticker', '')
        entry_price = float(stock.get('triggerPrice', 0))
        pos_sizing = stock.get('positionSizing', {})
        target_price = float(pos_sizing.get('pivotTargetExit', entry_price * 1.20))
        stoploss_price = float(pos_sizing.get('strictStopLoss', entry_price * 0.92))

        if not ticker or entry_price <= 0:
            continue

        # CRITICAL: Skip if already ordered in angel_orders.json
        if ticker in ordered_stocks:
            print(f"  ⊘ {ticker}: Already has order in Angel One (skip)")
            continue

        # Skip if already in radar
        if any(t.get('ticker') == ticker and t.get('source') == 'Trendline' for t in radar_trades):
            print(f"  ⊘ {ticker}: Already in Radar (skip)")
            continue
        
        # Skip if already in Volume tab
        if any(v.get('symbol') == ticker for v in volume_stocks):
            print(f"  ⊘ {ticker}: Already in Volume tab (skip)")
            continue
        
        # Skip if already in Trendline tab
        if any(g.get('symbol') == ticker for g in golden_stocks):
            print(f"  ⊘ {ticker}: Already in Trendline tab (skip)")
            continue

        # Get live price
        current_price = get_live_price(ticker)
        if not current_price:
            continue

        print(f"  {ticker}: Entry ₹{entry_price:.2f} | Current ₹{current_price:.2f}")

        # Check if price hit entry (within ±2% of entry price)
        lower = entry_price * 0.98
        upper = entry_price * 1.02
        if lower <= current_price <= upper:
            print(f"  ✅ Entry hit! Sending Telegram alert only (user takes trade manually)")
            # Telegram alert only — do NOT add to radar_trades.json
            send_telegram_with_action(
                ticker=ticker,
                entry_price=entry_price,
                current_price=current_price,
                target_price=target_price,
                stoploss_price=stoploss_price,
                source='Trendline'
            )

    # Trendline stocks only trigger Telegram alerts — user takes trades manually
    return False


def monitor_radar_positions():
    """Monitor Radar stocks for target/stoploss hits"""
    print("\n🎯 Monitoring Radar positions...")
    radar_trades = load_radar()
    if not radar_trades:
        print("  No trades in Radar")
        return False

    triggered_trades = [t for t in radar_trades if t.get('status') == 'Triggered']
    if not triggered_trades:
        print("  No triggered trades to monitor")
        return False

    print(f"  Monitoring {len(triggered_trades)} triggered trades...")
    changed = False

    for trade in radar_trades:
        if trade.get('status') != 'Triggered':
            continue

        ticker = trade.get('ticker', '')
        entry_price = float(trade.get('entry_price', 0))
        target_price = float(trade.get('target', entry_price * 1.20))
        stoploss_price = float(trade.get('stop_loss', entry_price * 0.92))
        source = trade.get('source', 'Unknown')

        # Get live price
        current_price = get_live_price(ticker)
        if not current_price:
            continue

        pnl_pct = ((current_price - entry_price) / entry_price) * 100
        print(f"  {ticker}: Entry ₹{entry_price:.2f} | Current ₹{current_price:.2f} | P&L {pnl_pct:+.2f}%")

        # Check if position should be closed (target or stoploss hit)
        if current_price >= target_price or current_price <= stoploss_price:
            exit_reason = "Target Hit" if current_price >= target_price else "Stop Loss Hit"
            trade['status'] = 'Closed'
            trade['exit_price'] = round(current_price, 2)
            trade['closed_at'] = datetime.now().isoformat()
            trade['pnl_pct'] = round(pnl_pct, 2)
            trade['exit_reason'] = exit_reason
            changed = True

            icon = "🎯" if current_price >= target_price else "🛑"
            msg = (
                f"{icon} *POSITION CLOSED - {exit_reason}*\n\n"
                f"Stock: *{ticker}*\n"
                f"Entry: ₹{entry_price:,.2f}\n"
                f"Exit: ₹{current_price:,.2f}\n"
                f"P&L: *{pnl_pct:+.2f}%*\n"
                f"Source: {source}\n"
                f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M IST')}"
            )
            send_telegram(msg)
            print(f"  ✅ {exit_reason} - Telegram sent!")

        else:
            # Still open - update current price
            trade['current_price'] = round(current_price, 2)
            trade['current_pnl_pct'] = round(pnl_pct, 2)
            changed = True

    if changed:
        save_radar(radar_trades)
    return changed


def sync_angel_one_positions():
    """Fetch open positions from Angel One via EC2 and REPLACE radar_trades.json with only current positions.
    Uses actual SELL order prices from Angel One order book for accurate exit P&L."""
    print("\n🔄 Syncing Angel One open positions...")
    try:
        response = requests.get('https://32-194-58-75.nip.io/api/sync-trades',
                                timeout=15, verify=False)
        if response.status_code != 200:
            print(f"  ⚠️ Sync endpoint returned {response.status_code}")
            return False

        data = response.json()
        if not data.get('success'):
            print(f"  ⚠️ Sync failed: {data.get('error')}")
            return False

        angel_positions = data.get('open_trades', [])
        # sell_exit_map: {TICKER: actual_exit_price} from Angel One order book
        sell_exit_map   = data.get('sell_exit_map', {})
        closed_today    = data.get('closed_trades', [])

        print(f"  Angel One open positions : {len(angel_positions)}")
        if sell_exit_map:
            print(f"  Sell exits from order book: {sell_exit_map}")

        existing = load_radar()
        order_trades = {t['order_id']: t for t in existing if t.get('order_id')}

        new_radar = []
        for pos in angel_positions:
            ticker = pos.get('ticker', '').replace('-EQ', '')
            if not ticker:
                continue

            entry_price  = float(pos.get('entry_price', pos.get('current_price', 0)) or 0)
            saved_target = float(pos.get('target', 0) or 0)
            saved_sl     = float(pos.get('stop_loss', 0) or 0)
            target       = saved_target if saved_target > 0 else round(entry_price * 1.25, 2)
            stop_loss    = saved_sl     if saved_sl     > 0 else round(entry_price * 0.93, 2)

            existing_entry = next((t for t in existing if t.get('ticker') == ticker
                                   and t.get('source') in ('Angel One', 'Groww', 'angel one')), None)
            if existing_entry:
                target    = float(existing_entry.get('target', target) or target) or target
                stop_loss = float(existing_entry.get('stop_loss', stop_loss) or stop_loss) or stop_loss

            new_radar.append({
                'ticker':          ticker,
                'entry_price':     entry_price,
                'current_price':   float(pos.get('current_price', 0) or 0),
                'target':          target,
                'stop_loss':       stop_loss,
                'quantity':        int(pos.get('quantity', 0) or 0),
                'status':          'Open',
                'source':          'Angel One',
                'is_angel_synced': True,
                'triggered_at':    (existing_entry or {}).get('triggered_at', datetime.now().isoformat())
            })
            print(f"  ✅ {ticker}: entry=₹{entry_price}, target=₹{target}, sl=₹{stop_loss}")

        # Add back order_id trades not in current positions
        for order_id, t in order_trades.items():
            if not any(r['ticker'] == t['ticker'] for r in new_radar):
                new_radar.append(t)

        # ── Handle auto-closed trades using ACTUAL exit prices ────────────
        # For each trade that was in radar but is now gone from holdings,
        # look up its actual exit price from the Angel One order book.
        if sell_exit_map:
            existing_open_tickers = {t.get('ticker', '').upper() for t in existing
                                     if t.get('status') in ('Open', 'Triggered', None, '')}
            new_open_tickers      = {t.get('ticker', '').upper() for t in new_radar}

            for ticker_up, exit_price in sell_exit_map.items():
                # Stock was open in radar but not in new holdings → sold today
                if ticker_up in existing_open_tickers and ticker_up not in new_open_tickers:
                    old_trade = next((t for t in existing
                                      if t.get('ticker', '').upper() == ticker_up), None)
                    if old_trade:
                        entry  = float(old_trade.get('entry_price', 0) or 0)
                        qty    = int(old_trade.get('quantity', 0) or 0)
                        pnl    = round((exit_price - entry) * qty, 2) if entry > 0 else 0
                        pnl_pct= round((exit_price - entry) / entry * 100, 2) if entry > 0 else 0
                        outcome= 'Target Hit' if pnl >= 0 else 'Stop Loss'
                        print(f"  🎯 Auto-closed {ticker_up}: entry=₹{entry} "
                              f"exit=₹{exit_price} (from order book) P&L={pnl_pct:+.2f}%")
                        send_telegram(
                            f"{'🎯' if pnl >= 0 else '🛑'} *POSITION CLOSED — {ticker_up}*\n\n"
                            f"Entry : ₹{entry:,.2f}\n"
                            f"Exit  : ₹{exit_price:,.2f} _(Angel One order book)_\n"
                            f"P&L   : *{pnl_pct:+.2f}%* (₹{pnl:+,.0f})\n"
                            f"Qty   : {qty} shares\n"
                            f"Time  : {datetime.now().strftime('%d %b %Y %H:%M IST')}"
                        )

        save_radar(new_radar)
        print(f"  ✅ radar_trades.json rebuilt with {len(new_radar)} Angel One positions")
        return True

    except Exception as e:
        print(f"  ⚠️ Angel One sync error: {e}")
        return False


def main():
    print(f"\n{'='*60}")
    print(f"TRADE MONITOR - {datetime.now().strftime('%Y-%m-%d %H:%M IST')}")
    print(f"{'='*60}")

    # ── Step 1: Clean radar_trades.json — keep only broker-synced trades ──
    print("\n🧹 Cleaning radar_trades.json...")
    allowed_sources = {'Angel One', 'angel one', 'Groww', 'groww', 'LocalTest', 'Telegram'}
    radar = load_radar()
    before = len(radar)
    radar = [t for t in radar if (
        t.get('order_id') or
        t.get('is_angel_synced') or
        t.get('source', '') in allowed_sources
    )]
    after = len(radar)
    if after != before:
        save_radar(radar)
        print(f"  Cleaned: {before} → {after} trades (removed {before-after} non-broker entries)")
    else:
        print(f"  No cleanup needed ({after} trades)")

    # Sync Angel One open positions to Radar first
    angel_changed = sync_angel_one_positions()

    # Check GTT exit orders (target/SL triggered)
    gtt_changed = check_gtt_exits()

    # Check Trendline stocks for entry hits ONLY
    tl_changed = check_trendline_stocks_for_entry()

    # Monitor Radar positions for target/stoploss
    radar_changed = monitor_radar_positions()

    if angel_changed or gtt_changed or tl_changed or radar_changed:
        print(f"\n✅ Updates saved to radar_trades.json")
    else:
        print(f"\n✅ No changes")

    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
