"""
notify_new_stocks.py
Sends Telegram alerts ONLY for stocks that are NEW since the last run.

Logic:
- Keeps a snapshot of previous run's stock lists in previous_stocks.json
- Compares current scan vs previous scan
- Only alerts on stocks that appear for the FIRST TIME
- Order confirmations are always sent (handled separately in angel_trade.py)
"""
import json
import os
import requests
from datetime import datetime

BASE_URL = "https://anuragsin17-sketch.github.io/Stock-Yard-Public"
PREV_FILE = "previous_stocks.json"


def send_telegram(message: str, reply_markup: dict = None) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("⚠️  Telegram not configured — skipping")
        return False
    try:
        payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json=payload,
            timeout=10,
        )
        if resp.status_code == 200:
            print("✅ Telegram sent")
            return True
        print(f"❌ Telegram failed: {resp.text[:200]}")
    except Exception as e:
        print(f"❌ Telegram error: {e}")
    return False


def load_previous() -> dict:
    """Load previous run's stock symbols {volume: set, trendline: set}"""
    if os.path.exists(PREV_FILE):
        try:
            with open(PREV_FILE) as f:
                data = json.load(f)
            return {
                "volume":    set(data.get("volume", [])),
                "trendline": set(data.get("trendline", []))
            }
        except Exception:
            pass
    return {"volume": set(), "trendline": set()}


def save_current(volume_symbols: set, trendline_symbols: set):
    """Save current run's stock symbols for next comparison"""
    with open(PREV_FILE, "w") as f:
        json.dump({
            "volume":    list(volume_symbols),
            "trendline": list(trendline_symbols),
            "updated_at": datetime.now().isoformat()
        }, f, indent=2)


def notify_volume_new(prev_volume: set) -> tuple:
    """Alert only NEW volume breakout stocks. Returns (sent_count, current_symbols)"""
    try:
        with open("data.json") as f:
            data = json.load(f)
    except Exception as e:
        print(f"⚠️  Could not load data.json: {e}")
        return 0, set()

    stocks = data.get("volume_breakout_stocks", [])
    current_symbols = {s.get("symbol", "") for s in stocks if s.get("symbol")}
    new_symbols = current_symbols - prev_volume

    print(f"   Volume: {len(current_symbols)} total, {len(new_symbols)} new → {new_symbols or 'none'}")
    sent = 0

    for s in stocks:
        sym = s.get("symbol", "")
        if sym not in new_symbols:
            continue

        ratio    = s.get("breakout_volume_ratio", 0)
        price    = s.get("current_price", 0)
        trigger  = s.get("radar_trigger_price", price)
        stop     = round(trigger * 0.92, 2)
        target   = round(trigger * 1.20, 2)
        breakout_date = s.get("breakout_date", "")
        company  = s.get("company_name", sym)
        chart_url = f"https://in.tradingview.com/chart/?symbol=NSE:{sym}"

        msg = (
            f"🔥 *NEW VOLUME BREAKOUT — {sym}*\n"
            f"_{company}_\n\n"
            f"📊 Volume Spike: *{ratio}x*\n"
            f"💰 Current Price: ₹{price:,.2f}\n"
            f"🎯 Entry Trigger: ₹{trigger:,.2f}\n"
            f"🛑 Stop Loss: ₹{stop:,.2f} _(monthly close)_\n"
            f"✅ Target: ₹{target:,.2f} _(+20%)_\n"
            f"📅 Breakout Date: {breakout_date}\n\n"
            f"[📉 View Chart]({chart_url}) | [📱 Open App]({BASE_URL}/)"
        )

        if send_telegram(msg):
            sent += 1

    return sent, current_symbols


def notify_trendline_new(prev_trendline: set) -> tuple:
    """Alert only NEW trendline touch stocks. Returns (sent_count, current_symbols)"""
    try:
        with open("trendline_screen.json") as f:
            stocks = json.load(f)
    except Exception as e:
        print(f"⚠️  Could not load trendline_screen.json: {e}")
        return 0, set()

    current_symbols = {s.get("ticker", "") for s in stocks if s.get("ticker")}
    new_symbols = current_symbols - prev_trendline

    print(f"   Trendline: {len(current_symbols)} total, {len(new_symbols)} new → {new_symbols or 'none'}")
    sent = 0

    for s in stocks:
        sym = s.get("ticker", "")
        if sym not in new_symbols:
            continue

        dist      = s.get("distanceRemaining", 99)
        price     = s.get("currentPrice", 0)
        trigger   = s.get("triggerPrice", price)
        stop      = s.get("positionSizing", {}).get("strictStopLoss", round(trigger * 0.92, 2))
        target    = s.get("positionSizing", {}).get("pivotTargetExit", round(trigger * 1.20, 2))
        fib       = s.get("fibLevelMatch", "—")
        score     = s.get("confluenceScore", "—")
        wicks     = s.get("wickTouches", "—")
        is_critical = s.get("notificationTrigger", False)

        status_emoji = "🎯" if is_critical else "📈"
        status_label = "CRITICAL ENTRY" if is_critical else "NEW SIGNAL"
        chart_url = f"https://in.tradingview.com/chart/?symbol=NSE:{sym}"
        confirm_url = (
            f"{BASE_URL}/?confirm={sym}"
            f"&price={trigger}&stop={stop}&target={target}&source=Trendline"
        )

        msg = (
            f"{status_emoji} *NEW TRENDLINE SIGNAL — {sym}* ({status_label})\n\n"
            f"💰 Current Price: ₹{price:,.2f}\n"
            f"📍 Entry Trigger: ₹{trigger:,.2f} _({dist:.2f}% away)_\n"
            f"🛑 Stop Loss: ₹{stop:,.2f} _(monthly close)_\n"
            f"✅ Target: ₹{target:,.2f} _(+20%)_\n"
            f"📐 Fib: {fib} | Score: {score}/10 | Wicks: {wicks}"
        )

        buttons = None
        if is_critical:
            buttons = {
                'inline_keyboard': [[
                    {'text': '✅ Confirm Trade', 'url': confirm_url},
                    {'text': '⏭️ Skip', 'url': f"{BASE_URL}/"}
                ]]
            }
        else:
            buttons = {
                'inline_keyboard': [[
                    {'text': '📉 View Chart', 'url': chart_url},
                    {'text': '📱 Open App', 'url': f"{BASE_URL}/"}
                ]]
            }

        if send_telegram(msg, reply_markup=buttons):
            sent += 1

    return sent, current_symbols


def main():
    print("=" * 60)
    print(f"STOCK YARD NOTIFIER — {datetime.now().strftime('%Y-%m-%d %H:%M IST')}")
    print("=" * 60)

    prev = load_previous()

    print("\n📊 Checking Volume breakouts...")
    vol_sent, vol_current = notify_volume_new(prev["volume"])

    print("\n📈 Checking Trendline signals...")
    tl_sent, tl_current = notify_trendline_new(prev["trendline"])

    # Save current state for next run comparison
    save_current(vol_current, tl_current)

    total = vol_sent + tl_sent
    print(f"\n✅ Done — {vol_sent} volume + {tl_sent} trendline NEW alerts sent")

    if total > 0:
        summary = (
            f"📋 *Stock Yard Update — {datetime.now().strftime('%d %b, %H:%M IST')}*\n\n"
            f"🔥 New Volume signals: {vol_sent}\n"
            f"📈 New Trendline signals: {tl_sent}\n\n"
            f"[Open App]({BASE_URL}/)"
        )
        send_telegram(summary)
    else:
        print("   No new stocks — no Telegram sent")


if __name__ == "__main__":
    main()
