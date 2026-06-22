#!/usr/bin/env python3
"""
Live Price Updater — Stock Yard
Runs every 60s during market hours. Fetches LTP for all active tickers
(trendline + volume + open radar trades) and writes to DynamoDB LivePrices table.

The dashboard then reads /api/prices in one bulk call instead of N individual calls.

Run as systemd service or cron:
  systemd: live-prices.service
  cron:    */1 9-16 * * 1-5 python3 /home/ubuntu/live_price_updater.py

NOTE: All time comparisons use IST (Asia/Kolkata) explicitly.
      EC2 may run on UTC — never rely on system timezone for market hours.
"""
import os
import sys
import time
import json
import pyotp
import logging
from datetime import datetime, time as dtime

try:
    from zoneinfo import ZoneInfo          # Python 3.9+
    IST = ZoneInfo('Asia/Kolkata')
except ImportError:
    try:
        import pytz
        IST = pytz.timezone('Asia/Kolkata')
    except ImportError:
        IST = None  # fallback: assume system time is IST

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

RADAR_FILE    = '/home/ubuntu/radar_trades.json'
TRENDLINE_FILE = '/home/ubuntu/stock-yard-backend/trendline_screen.json'
DATA_FILE     = '/home/ubuntu/stock-yard-backend/data.json'
# Persists post-close fetch state across restarts; keyed by YYYY-MM-DD (IST)
CLOSE_FETCH_STATE_FILE = '/tmp/live_price_close_fetch.json'
UPDATE_INTERVAL = 60  # seconds

MARKET_OPEN  = dtime(9, 15)
MARKET_CLOSE = dtime(15, 35)
# Allow post-close fetches until 19:30 IST so service restarts after 16:00 still work
POST_CLOSE_CUTOFF = dtime(19, 30)


def _now_ist():
    """Return current datetime in IST regardless of system timezone."""
    if IST is not None:
        return datetime.now(tz=IST)
    return datetime.now()


def is_market_hours():
    now = _now_ist().time()
    return MARKET_OPEN <= now <= MARKET_CLOSE


def is_post_close_window():
    """True between market close (15:35) and POST_CLOSE_CUTOFF (18:30) IST."""
    now = _now_ist().time()
    return MARKET_CLOSE < now <= POST_CLOSE_CUTOFF


def _load_close_fetch_state():
    """Return today's IST date string if post-close fetch was already done, else None."""
    try:
        with open(CLOSE_FETCH_STATE_FILE) as f:
            state = json.load(f)
        return state.get('done_date')
    except Exception:
        return None


def _mark_close_fetch_done():
    """Persist today's IST date so restarts know the post-close fetch already ran."""
    today = _now_ist().strftime('%Y-%m-%d')
    try:
        with open(CLOSE_FETCH_STATE_FILE, 'w') as f:
            json.dump({'done_date': today}, f)
    except Exception as e:
        logger.warning(f"Could not persist close-fetch state: {e}")


def load_active_tickers():
    """Collect all tickers that need live prices AND write signals to DynamoDB."""
    tickers = set()

    # Open radar trades
    try:
        with open(RADAR_FILE) as f:
            trades = json.load(f)
        for t in trades:
            if t.get('status') in ('Open', 'Triggered') and t.get('ticker'):
                tickers.add(t['ticker'].upper().replace('-EQ', ''))
    except Exception as e:
        logger.warning(f"Radar file read error: {e}")

    # Trendline stocks — load and write to DynamoDB StockSignals
    try:
        with open(TRENDLINE_FILE) as f:
            stocks = json.load(f)
        for s in (stocks or []):
            ticker = (s.get('ticker') or s.get('symbol') or '').upper().replace('-EQ', '')
            if ticker:
                tickers.add(ticker)
        # Write trendline signals to DynamoDB (non-blocking — ignore errors)
        try:
            sys.path.insert(0, '/home/ubuntu/stock-yard-backend')
            import dynamodb_helper as dh
            dh.write_signals('TRENDLINE', stocks or [])
        except Exception as e:
            logger.debug(f"DynamoDB trendline write skipped: {e}")
    except Exception as e:
        logger.warning(f"Trendline file read error: {e}")

    # Volume watchlist — load from volume_gainer_watchlist.json and write to DynamoDB
    VOLUME_FILE = '/home/ubuntu/stock-yard-backend/volume_gainer_watchlist.json'
    if not os.path.exists(VOLUME_FILE):
        VOLUME_FILE = '/home/ubuntu/volume_gainer_watchlist.json'
    try:
        with open(VOLUME_FILE) as f:
            vol_stocks = json.load(f)
        for s in (vol_stocks or []):
            ticker = (s.get('ticker') or s.get('symbol') or '').upper().replace('-EQ', '')
            if ticker:
                tickers.add(ticker)
        # Write volume signals to DynamoDB
        try:
            sys.path.insert(0, '/home/ubuntu/stock-yard-backend')
            import dynamodb_helper as dh
            dh.write_signals('VOLUME', vol_stocks or [])
        except Exception as e:
            logger.debug(f"DynamoDB volume write skipped: {e}")
    except Exception as e:
        logger.debug(f"Volume file read: {e}")

    # Also load from old data.json as fallback for volume
    try:
        with open(DATA_FILE) as f:
            data = json.load(f)
        stocks = data if isinstance(data, list) else (
            data.get('volume_gainer_stocks') or data.get('volume_breakout_stocks') or []
        )
        for s in (stocks or []):
            ticker = (s.get('ticker') or s.get('symbol') or '').upper().replace('-EQ', '')
            if ticker:
                tickers.add(ticker)
    except Exception as e:
        logger.warning(f"Data file read error: {e}")

    return tickers


def get_angel_session():
    """Authenticate with Angel One SmartAPI."""
    from SmartApi import SmartConnect
    creds = {k: os.environ.get(k, '') for k in
             ['ANGEL_API_KEY', 'ANGEL_CLIENT_ID', 'ANGEL_PASSWORD', 'ANGEL_TOTP_SECRET']}
    if not all(creds.values()):
        logger.error("Missing Angel One credentials in environment")
        return None
    try:
        smart = SmartConnect(api_key=creds['ANGEL_API_KEY'])
        totp  = pyotp.TOTP(creds['ANGEL_TOTP_SECRET']).now()
        sess  = smart.generateSession(creds['ANGEL_CLIENT_ID'], creds['ANGEL_PASSWORD'], totp)
        if isinstance(sess, dict) and sess.get('status'):
            logger.info("Angel One session OK")
            return smart
        logger.error(f"Session failed: {sess}")
    except Exception as e:
        logger.error(f"Session error: {e}")
    return None


def fetch_ltps(tickers: set) -> dict:
    """Fetch LTP for all tickers.
    Primary: local Angel One /api/get-quote (real-time intraday LTP).
    Fallback: yfinance 1-minute interval for the latest trade price.
    Note: yfinance 1d/daily interval gives EOD close — NOT live intraday price.
    """
    prices = {}
    ticker_list = sorted(tickers)

    # --- Primary: local Angel One /api/get-quote (real-time) ---
    LOCAL_QUOTE_API = 'http://127.0.0.1:5000/api/get-quote'
    failed_tickers = []
    for ticker in ticker_list:
        try:
            r = requests.get(LOCAL_QUOTE_API, params={'symbol': ticker}, timeout=10)
            if r.status_code == 200:
                d = r.json()
                if d.get('success'):
                    ltp = float(d.get('ltp', 0))
                    if ltp > 0:
                        prices[ticker] = ltp
                        continue
            failed_tickers.append(ticker)
        except Exception:
            failed_tickers.append(ticker)

    if prices:
        logger.info(f"Angel One /api/get-quote: {len(prices)} prices fetched, {len(failed_tickers)} failed")

    # --- Fallback for failed tickers: yfinance 1-minute interval ---
    if failed_tickers:
        try:
            import yfinance as yf
            yf_symbols = [t + '.NS' for t in failed_tickers]
            # Use 1m interval over 1d period to get latest intraday trade price
            data = yf.download(
                yf_symbols,
                period='1d',
                interval='1m',
                auto_adjust=True,
                progress=False,
                threads=True
            )
            if not data.empty:
                close = data['Close'] if 'Close' in data else None
                if close is not None:
                    last_row = close.dropna(how='all').iloc[-1] if len(close.dropna(how='all')) > 0 else None
                    if last_row is not None:
                        for sym in yf_symbols:
                            ticker = sym.replace('.NS', '')
                            val = last_row.get(sym)
                            if val is not None and float(val) > 0:
                                prices[ticker] = round(float(val), 2)
            logger.info(f"yfinance fallback: recovered {sum(1 for t in failed_tickers if t in prices)}/{len(failed_tickers)}")
        except Exception as e:
            logger.warning(f"yfinance fallback failed: {e}")

    return prices


def write_to_dynamodb(prices: dict):
    """Write bulk prices to DynamoDB LivePrices table."""
    try:
        sys.path.insert(0, '/home/ubuntu/stock-yard-backend')
        import dynamodb_helper as dh
        dh.write_prices_bulk(prices)
    except Exception as e:
        logger.error(f"DynamoDB write error: {e}")


def main():
    logger.info("Live Price Updater starting...")
    if IST is not None:
        logger.info(f"Timezone: IST (Asia/Kolkata). Current IST time: {_now_ist().strftime('%H:%M:%S')}")
    else:
        logger.warning("pytz/zoneinfo not available — using system time. Ensure TZ=Asia/Kolkata is set.")

    while True:
        try:
            now_ist = _now_ist()
            now_time = now_ist.time()
            in_market = is_market_hours()
            in_post_close = is_post_close_window()

            if not in_market and not in_post_close:
                logger.info(f"Outside market hours (IST {now_time.strftime('%H:%M')}) — sleeping 5 min")
                time.sleep(300)
                continue

            # Post-close: only do one fetch per day (persisted across restarts)
            if in_post_close and not in_market:
                today_ist = now_ist.strftime('%Y-%m-%d')
                done_date = _load_close_fetch_state()
                if done_date == today_ist:
                    logger.info("Post-close fetch done for today — sleeping 5 min")
                    time.sleep(300)
                    continue
                logger.info(f"Post-close window (IST {now_time.strftime('%H:%M')}): fetching closing prices...")

            tickers = load_active_tickers()
            logger.info(f"Fetching LTP for {len(tickers)} tickers...")

            prices = fetch_ltps(tickers)
            logger.info(f"Got {len(prices)} prices")

            if prices:
                write_to_dynamodb(prices)
                if in_post_close and not in_market:
                    _mark_close_fetch_done()
                    logger.info("Post-close fetch complete — closing prices stored in DynamoDB")

            # Also write to local cache file for fallback
            cache_file = '/home/ubuntu/live_prices_cache.json'
            try:
                with open(cache_file, 'w') as f:
                    json.dump({'timestamp': datetime.now().isoformat(), 'prices': prices}, f)
            except Exception:
                pass

        except Exception as e:
            logger.error(f"Main loop error: {e}", exc_info=True)

        time.sleep(UPDATE_INTERVAL)


if __name__ == '__main__':
    main()
