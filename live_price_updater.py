#!/usr/bin/env python3
"""
Live Price Updater — Stock Yard
Runs every 60s during market hours. Fetches LTP for all active tickers
(trendline + volume + open radar trades) and writes to DynamoDB LivePrices table.

The dashboard then reads /api/prices in one bulk call instead of N individual calls.

Run as systemd service or cron:
  systemd: live-prices.service
  cron:    */1 9-16 * * 1-5 python3 /home/ubuntu/live_price_updater.py
"""
import os
import sys
import time
import json
import pyotp
import logging
from datetime import datetime, time as dtime

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

RADAR_FILE    = '/home/ubuntu/radar_trades.json'
TRENDLINE_FILE = '/home/ubuntu/stock-yard-backend/trendline_screen.json'
DATA_FILE     = '/home/ubuntu/stock-yard-backend/data.json'
UPDATE_INTERVAL = 60  # seconds

MARKET_OPEN  = dtime(9, 15)
MARKET_CLOSE = dtime(15, 35)
CLOSE_FETCH_DONE = False  # run one post-close fetch per day


def is_market_hours():
    now = datetime.now().time()
    return MARKET_OPEN <= now <= MARKET_CLOSE


def is_post_close_window():
    """True for 30 min after market close — do one final price fetch."""
    now = datetime.now().time()
    return MARKET_CLOSE <= now <= dtime(16, 0)


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


def fetch_ltps(smart, tickers: set) -> dict:
    """Fetch LTP for all tickers from Angel One. Returns {ticker: ltp}."""
    prices = {}
    ticker_list = sorted(tickers)

    # Angel One getQuote handles one ticker at a time — batch with small sleep
    for ticker in ticker_list:
        try:
            quote = smart.getQuote('NSE', ticker + '-EQ')
            if isinstance(quote, dict) and quote.get('status'):
                data = quote.get('data', {})
                if isinstance(data, list) and data:
                    data = data[0]
                ltp = float(data.get('ltp', 0))
                if ltp > 0:
                    prices[ticker] = ltp
            time.sleep(0.05)  # 50ms between calls to avoid rate limit
        except Exception as e:
            logger.debug(f"Quote error {ticker}: {e}")

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

    smart = None
    session_ts = 0

    while True:
        try:
            now_time = datetime.now().time()
            in_market = is_market_hours()
            in_post_close = is_post_close_window()

            global CLOSE_FETCH_DONE

            # Reset flag each morning
            if now_time < MARKET_OPEN:
                CLOSE_FETCH_DONE = False

            if not in_market and not in_post_close:
                logger.info("Outside market hours — sleeping 5 min")
                time.sleep(300)
                continue

            # Post-close: only do one fetch per day
            if in_post_close and not in_market:
                if CLOSE_FETCH_DONE:
                    logger.info("Post-close fetch done for today — sleeping 5 min")
                    time.sleep(300)
                    continue
                logger.info("Post-close: fetching closing prices for DynamoDB...")

            # Refresh session every 4 hours
            now_ts = time.time()
            if smart is None or (now_ts - session_ts) > 14400:
                smart = get_angel_session()
                session_ts = now_ts
                if not smart:
                    logger.error("Cannot get session — sleeping 60s")
                    time.sleep(60)
                    continue

            tickers = load_active_tickers()
            logger.info(f"Fetching LTP for {len(tickers)} tickers...")

            prices = fetch_ltps(smart, tickers)
            logger.info(f"Got {len(prices)} prices")

            if prices:
                write_to_dynamodb(prices)
                if in_post_close and not in_market:
                    CLOSE_FETCH_DONE = True
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
