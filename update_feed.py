import json
import pandas as pd
import os
import requests
import yfinance as yf
from datetime import datetime
from geometric_engine import MacroInstitutionalEngine

BASE_URL = "https://anuragsin17-sketch.github.io/Stock-Yard-Public"

def send_telegram_alert(message: str, reply_markup: dict = None) -> None:
    telegram_bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
    telegram_chat_id = os.getenv('TELEGRAM_CHAT_ID')
    if not telegram_bot_token or not telegram_chat_id:
        print("Telegram not configured")
        return
    try:
        url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
        payload = {
            'chat_id': telegram_chat_id,
            'text': message,
            'parse_mode': 'Markdown'
        }
        if reply_markup:
            payload['reply_markup'] = reply_markup
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            print("Telegram alert sent")
        else:
            print(f"Telegram failed: {response.text}")
    except Exception as e:
        print(f"Telegram error: {e}")

def calculate_emas(ticker: str) -> dict:
    """Calculate 50 EMA and 200 EMA for a given ticker"""
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period="1y")  # Get 1 year of data for EMA calculation
        
        if hist.empty or len(hist) < 200:
            return {"ema50": None, "ema200": None}
        
        # Calculate EMAs - get the last value
        ema50_series = hist['Close'].ewm(span=50, adjust=False).mean()
        ema200_series = hist['Close'].ewm(span=200, adjust=False).mean()
        
        ema50 = ema50_series.iloc[-1]
        ema200 = ema200_series.iloc[-1]
        
        return {
            "ema50": round(float(ema50), 2) if pd.notna(ema50) else None,
            "ema200": round(float(ema200), 2) if pd.notna(ema200) else None
        }
    except Exception as e:
        print(f"   EMA calculation error for {ticker}: {e}")
        return {"ema50": None, "ema200": None}


def synchronize_production_database():
    engine = MacroInstitutionalEngine(position_size=50000.0, sl_pct=8.0, touch_tolerance=2.0)
    print("Connecting to NSE network...")

    try:
        df = pd.read_csv('Stock List.csv')
        tickers = [str(t).strip() + ".NS" for t in df['Symbol'].tolist() if str(t).strip()]
        print(f"Loaded {len(tickers)} tickers")
    except Exception as e:
        print(f"CSV load failed: {e}, using fallback")
        tickers = ["SBIN.NS", "AXISBANK.NS", "INFY.NS", "RELIANCE.NS", "HDFCBANK.NS",
                   "TCS.NS", "ICICIBANK.NS", "KOTAKBANK.NS", "LT.NS", "TITAN.NS"]

    compiled_screen_data = []
    critical_alerts = []
    watchlist_alerts = []

    # Load already-alerted stocks today (prevent spam)
    alerted_today_file = 'alerted_today.json'
    today_str = datetime.now().strftime('%Y-%m-%d')
    alerted_today = {}
    if os.path.exists(alerted_today_file):
        try:
            with open(alerted_today_file, 'r') as f:
                data = json.load(f)
                if data.get('date') == today_str:
                    alerted_today = data.get('stocks', {})
        except Exception:
            alerted_today = {}

    print(f"Scanning {len(tickers)} stocks...")

    for count, stock in enumerate(tickers, 1):
        try:
            data = engine.process_ticker_geometry(stock)
            if data:
                # Calculate EMAs
                emas = calculate_emas(stock)
                
                record = {
                    "ticker": data["ticker"],
                    "currentPrice": data["currentSignal"]["currentPrice"],
                    "ema50": emas["ema50"],
                    "ema200": emas["ema200"],
                    "triggerPrice": data["currentSignal"]["triggerPrice"],
                    "distanceRemaining": data["currentSignal"]["distanceRemaining"],
                    "fibLevelMatch": data["currentSignal"].get("fibLevelMatch", "N/A"),
                    "patternZone": data["currentSignal"].get("confluenceNote", "Trendline Support"),
                    "notificationTrigger": data["currentSignal"]["notificationTrigger"],
                    "positionSizing": {
                        "sharesToBuy": data["positionSizing"]["sharesToBuy"],
                        "strictStopLoss": data["positionSizing"]["dynamicStopLoss"],
                        "pivotTargetExit": data["positionSizing"]["targetExit"],
                        "allocatedAmount": 50000.0
                    },
                    "wickTouches": data["trendlineDetails"]["wickTouches"],
                    "timeframe": data["trendlineDetails"].get("timeframe", "monthly"),
                    "confluenceScore": data["currentSignal"]["confluenceScore"]
                }

                compiled_screen_data.append(record)
                ema_info = f"50EMA: ₹{emas['ema50']:.2f}" if emas['ema50'] else "50EMA: N/A"
                ema_info += f" | 200EMA: ₹{emas['ema200']:.2f}" if emas['ema200'] else " | 200EMA: N/A"
                print(f"   [+] {data['ticker']:12} | Rs{record['currentPrice']:8.2f} | Trigger: Rs{record['triggerPrice']:8.2f} | Dist: {record['distanceRemaining']:.2f}% | {ema_info}")

                if data["currentSignal"]["notificationTrigger"]:
                    critical_alerts.append(data['ticker'])
                elif record['distanceRemaining'] <= 2.0:
                    watchlist_alerts.append(data['ticker'])

        except Exception as e:
            print(f"   Error {stock}: {e}")
            continue

    # Sort by distance
    compiled_screen_data.sort(key=lambda x: x['distanceRemaining'])

    # Write JSON for HTML dashboard
    with open("trendline_screen.json", "w") as f:
        json.dump(compiled_screen_data, f, indent=4)

    print(f"\n==================================================================")
    print(f"SUCCESS: {len(compiled_screen_data)} signals | Critical: {len(critical_alerts)} | Watchlist: {len(watchlist_alerts)}")
    print(f"==================================================================")

    # Send individual critical alerts - ONLY if not already alerted today
    new_alerts = []
    for record in compiled_screen_data:
        if record['notificationTrigger']:
            ticker = record['ticker']

            # Skip if already alerted today (prevent spam)
            if ticker in alerted_today:
                print(f"   Skipping {ticker} - already alerted today at {alerted_today[ticker]}")
                continue

            price = record['triggerPrice']
            stop = record['positionSizing']['strictStopLoss']
            target = record['positionSizing']['pivotTargetExit']
            qty = record['positionSizing']['sharesToBuy']
            dist = record['distanceRemaining']

            # Deep link - opens dashboard and auto-triggers confirm dialog
            confirm_url = (
                f"{BASE_URL}/?confirm={ticker}"
                f"&price={price}&qty={qty}"
                f"&stop={stop}&target={target}&source=Trendline"
            )

            alert = (
                f"🎯 *CRITICAL TRENDLINE ENTRY*\n\n"
                f"Stock: *{ticker}*\n"
                f"Current: Rs{record['currentPrice']:,.2f}\n"
                f"Trigger: Rs{price:,.2f} ({dist:.2f}% away)\n"
                f"Stop Loss: Rs{stop:,.2f} (Monthly Close)\n"
                f"Target: Rs{target:,.2f} (+20%)\n"
                f"Qty: {qty} shares"
            )

            # Inline keyboard - Confirm Trade opens dashboard, Skip does nothing
            buttons = {
                'inline_keyboard': [[
                    {
                        'text': '✅ Confirm Trade',
                        'url': confirm_url
                    },
                    {
                        'text': '⏭️ Skip',
                        'url': f"{BASE_URL}/"
                    }
                ]]
            }

            send_telegram_alert(alert, reply_markup=buttons)
            new_alerts.append(ticker)
            alerted_today[ticker] = datetime.now().strftime('%H:%M')
            print(f"   Critical alert sent for {ticker}")

    # Save alerted_today to prevent spam on next run
    with open(alerted_today_file, 'w') as f:
        json.dump({'date': today_str, 'stocks': alerted_today}, f)

    # Send summary only if there are signals
    if compiled_screen_data:
        summary = (
            f"Trendline Scanner Summary\n\n"
            f"{datetime.now().strftime('%Y-%m-%d %H:%M IST')}\n"
            f"Total signals: {len(compiled_screen_data)}\n"
            f"Critical (+-1%): {len(critical_alerts)}\n"
            f"Watchlist (+-2%): {len(watchlist_alerts)}\n"
            f"New alerts: {len(new_alerts)}\n"
        )
        if critical_alerts:
            summary += f"\nCritical: {', '.join(critical_alerts)}"
        if watchlist_alerts:
            summary += f"\nWatchlist: {', '.join(watchlist_alerts[:5])}"
        summary += f"\n\nView Full Report:\n{BASE_URL}/"
        send_telegram_alert(summary)

if __name__ == "__main__":
    synchronize_production_database()
