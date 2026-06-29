#!/usr/bin/env python3
"""
Angel One Order Handler API
Receives order confirmation from dashboard and places on Angel One
Designed to run as a systemd service on EC2 with environment variable credentials
v2.1 — order book exit price lookup for accurate closed trade P&L
"""

import os
import sys
import json
import secrets
import pyotp
import logging
import requests
import pandas as pd
from SmartApi import SmartConnect
from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime, timedelta

# Configure logging for systemd — force UTF-8 so emojis render correctly
import io
utf8_stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    stream=utf8_stdout
)
logger = logging.getLogger(__name__)

# Cached Angel One session (avoids re-auth on every request)
_angel_session = None
_angel_session_expiry = 0

app = Flask(__name__)

# API key for authenticating dashboard requests — set as env var on EC2
STOCKYARD_API_KEY = os.environ.get('STOCKYARD_API_KEY', '')

# Enable CORS — restrict order endpoints to GitHub Pages origin only
# Read-only endpoints (get-quote, signals, etc.) remain open for the dashboard
ALLOWED_ORIGINS = [
    "https://anuragsin17-sketch.github.io",
    "http://localhost",       # local dev
    "http://127.0.0.1",       # local dev
]

CORS(app, resources={
    r"/api/place-order": {
        "origins": ALLOWED_ORIGINS,
        "methods": ["POST", "OPTIONS"],
        "allow_headers": ["Content-Type", "X-API-Key"]
    },
    r"/api/order-status/*": {
        "origins": ALLOWED_ORIGINS,
        "methods": ["GET", "OPTIONS"],
        "allow_headers": ["Content-Type", "X-API-Key"]
    },
    r"/api/*": {
        "origins": ["*"],
        "methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": ["Content-Type", "X-API-Key"]
    },
    r"/health": {
        "origins": ["*"],
        "methods": ["GET", "OPTIONS"]
    }
})

def check_api_key():
    """Validate X-API-Key header. Returns True if valid."""
    if not STOCKYARD_API_KEY:
        # If key not configured on EC2, log warning but allow (backward compat)
        logger.warning("STOCKYARD_API_KEY not set — endpoint is unprotected")
        return True
    provided = request.headers.get('X-API-Key', '')
    if provided != STOCKYARD_API_KEY:
        logger.warning(f"Invalid API key attempt from {request.remote_addr}")
        return False
    return True

# Load Angel One credentials from environment variables
def load_credentials():
    """Load credentials from environment variables (for systemd service)"""
    credentials = {
        'ANGEL_API_KEY': os.environ.get('ANGEL_API_KEY'),
        'ANGEL_CLIENT_ID': os.environ.get('ANGEL_CLIENT_ID'),
        'ANGEL_PASSWORD': os.environ.get('ANGEL_PASSWORD'),
        'ANGEL_TOTP_SECRET': os.environ.get('ANGEL_TOTP_SECRET'),
    }
    
    # Verify all required credentials are present
    missing = [k for k, v in credentials.items() if not v]
    if missing:
        logger.warning(f"Missing credentials: {', '.join(missing)}")
        return None
    
    return credentials

def send_telegram_notification(message):
    """Send Telegram notification"""
    try:
        # Load from environment variables only — never hardcode tokens
        token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
        chat_id = os.environ.get('TELEGRAM_CHAT_ID', '')
        if not token or not chat_id:
            logger.warning("Telegram not configured (env vars missing)")
            return False
        
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            'chat_id': chat_id,
            'text': message,
            'parse_mode': 'Markdown'
        }
        
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            logger.info("? Telegram notification sent")
            return True
        else:
            logger.warning(f"? Telegram failed: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        logger.warning(f"? Telegram error: {e}")
        return False

def get_angel_session(force_refresh=False):
    """Get authenticated SmartConnect session — cached for 4 hours to avoid rate limits.
    Pass force_refresh=True to invalidate the cache and re-authenticate."""
    global _angel_session, _angel_session_expiry
    import time
    now = int(time.time())

    # Force refresh clears the cache (used when a cached session causes API errors)
    if force_refresh:
        logger.info("Forcing session refresh — clearing cached session")
        _angel_session = None
        _angel_session_expiry = 0

    # Return cached session if still valid
    if _angel_session and now < _angel_session_expiry:
        logger.info("Reusing cached Angel One session")
        return _angel_session

    credentials = load_credentials()
    if not credentials:
        logger.error("Cannot create session: credentials not configured")
        return None

    try:
        logger.info("Creating new SmartConnect session...")
        smart = SmartConnect(api_key=credentials.get('ANGEL_API_KEY'))
        totp = pyotp.TOTP(credentials.get('ANGEL_TOTP_SECRET')).now()
        session = smart.generateSession(
            credentials.get('ANGEL_CLIENT_ID'),
            credentials.get('ANGEL_PASSWORD'),
            totp
        )
        if not isinstance(session, dict) or not session.get('status'):
            logger.error(f"Session generation failed: {session}")
            return None

        _angel_session = smart
        _angel_session_expiry = now + 14400  # cache for 4 hours
        logger.info("Angel One session created and cached for 4 hours")
        return smart
    except Exception as e:
        logger.error(f"Session error: {e}", exc_info=True)
        return None

def get_account_balance(smart):
    """Fetch account balance and margin details from Angel One"""
    try:
        if not smart:
            logger.error("Cannot fetch balance: No session")
            return None
        
        logger.info("Fetching account balance...")
        
        # Try getProfile first
        try:
            profile = smart.getProfile()
            logger.info(f"Profile response: {profile}")
            
            if isinstance(profile, dict) and profile.get('status'):
                profile_data = profile.get('data', {})
                if isinstance(profile_data, list) and len(profile_data) > 0:
                    profile_data = profile_data[0]
                
                # Profile might have balance info
                if profile_data.get('cashavailable'):
                    balance_info = {
                        'cash_available': float(profile_data.get('cashavailable', 0)),
                        'margin_available': float(profile_data.get('marginavailable', 0)),
                        'total_margin': float(profile_data.get('totalmargin', 0)),
                        'margin_used': float(profile_data.get('marginused', 0)),
                        'total_balance': float(profile_data.get('totalbalance', 0))
                    }
                    logger.info(f"Account balance: Cash={balance_info['cash_available']}, Margin={balance_info['margin_available']}")
                    return balance_info
        except Exception as e:
            logger.warning(f"getProfile failed: {e}")
        
        # If profile doesn't have balance, try getRMS (Funds API)
        try:
            logger.info("Trying getRMS API for funds...")
            rms = smart.getRMS()
            logger.info(f"RMS response: {rms}")
            
            if isinstance(rms, dict) and rms.get('status'):
                rms_data = rms.get('data', {})
                if isinstance(rms_data, list) and len(rms_data) > 0:
                    rms_data = rms_data[0]
                
                balance_info = {
                    'cash_available': float(rms_data.get('net', 0)),
                    'margin_available': float(rms_data.get('available', 0)),
                    'total_margin': float(rms_data.get('grossavail', 0)),
                    'margin_used': float(rms_data.get('used', 0)),
                    'total_balance': float(rms_data.get('net', 0))
                }
                logger.info(f"Account balance (RMS): Available={balance_info['margin_available']}")
                return balance_info
        except Exception as e:
            logger.warning(f"getRMS failed: {e}")
        
        logger.error("Could not fetch balance from any API")
        return None
        
    except Exception as e:
        logger.error(f"Balance fetch error: {e}", exc_info=True)
        return None

def validate_order_funds(smart, quantity, entry_price, symbol):
    """Validate if account has sufficient funds to place the order"""
    try:
        balance_info = get_account_balance(smart)
        
        if not balance_info:
            # If we can't fetch balance, let the order attempt go through
            # Angel One will reject it if there's insufficient funds
            logger.warning("Cannot validate funds: Could not fetch balance - allowing order to proceed")
            return {
                'valid': True,  # Allow order to proceed, let Angel One handle validation
                'reason': 'Balance fetch failed - proceeding with order',
                'balance_info': None,
                'shortfall': 0
            }
        
        # Calculate order requirements
        order_value = quantity * entry_price
        margin_required = order_value  # For delivery orders, need full payment
        available_margin = balance_info.get('margin_available', 0)
        shortfall = max(0, margin_required - available_margin)
        
        is_valid = shortfall == 0
        
        logger.info(f"\n{'='*60}")
        logger.info(f"Order validation for {symbol}")
        logger.info(f"{'='*60}")
        logger.info(f"Order value: ?{order_value:,.2f}")
        logger.info(f"Margin required: ?{margin_required:,.2f}")
        logger.info(f"Margin available: ?{available_margin:,.2f}")
        
        if is_valid:
            logger.info(f"? Sufficient funds for order: {symbol}")
        else:
            logger.warning(f"? Insufficient funds for order: {symbol}")
            logger.warning(f"   Shortfall: ?{shortfall:,.2f}")
        
        logger.info(f"{'='*60}\n")
        
        return {
            'valid': is_valid,
            'reason': 'Sufficient funds' if is_valid else 'Insufficient margin available',
            'balance_info': balance_info,
            'shortfall': shortfall,
            'order_value': order_value
        }
    
    except Exception as e:
        logger.error(f"Order validation error: {e}", exc_info=True)
        # On error, allow order to proceed - let Angel One handle it
        return {
            'valid': True,
            'reason': f'Validation error (proceeding anyway): {str(e)}',
            'balance_info': None,
            'shortfall': 0
        }

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint - NO authentication required"""
    try:
        credentials = load_credentials()
        if credentials:
            logger.info("Health check: OK")
            return jsonify({'status': 'ok', 'service': 'angel-order-handler', 'timestamp': datetime.now().isoformat()}), 200
        else:
            logger.warning("Health check: Missing credentials")
            return jsonify({'status': 'error', 'service': 'angel-order-handler', 'error': 'Missing credentials'}), 503
    except Exception as e:
        logger.error(f"Health check error: {e}")
        return jsonify({'status': 'error', 'error': str(e)}), 500



@app.route('/api/place-order', methods=['POST'])
def place_order():
    """Place order on Angel One with pre-validation"""
    if not check_api_key():
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    try:
        data = request.json
        symbol = data.get('symbol')
        quantity = int(data.get('quantity', 1))
        entry_price = float(data.get('entry_price', 0))
        target_price = float(data.get('target_price', 0))
        stop_loss = float(data.get('stop_loss', 0))
        
        logger.info(f"Order request: {symbol} x{quantity} @ Rs{entry_price}")
        
        if not symbol:
            logger.warning("Order placement failed: No symbol provided")
            return jsonify({'success': False, 'error': 'No symbol provided'}), 400
        
        # Get session
        logger.info("Getting Angel One session...")
        smart = get_angel_session()
        if not smart:
            logger.error("Order placement failed: Could not connect to Angel One")
            return jsonify({'success': False, 'error': 'Failed to connect to Angel One'}), 401
        
        # ============ CRITICAL: VALIDATE FUNDS BEFORE PROCEEDING ============
        logger.info(f"Validating funds for order: {symbol}")
        validation = validate_order_funds(smart, quantity, entry_price, symbol)
        
        if not validation['valid']:
            logger.warning(f"Order validation failed for {symbol}: {validation['reason']}")
            
            # Send rejection Telegram notification
            order_value = entry_price * quantity
            shortfall_msg = f" (Need \u20b9{validation['shortfall']:,.0f} more)" if validation['shortfall'] > 0 else ""
            telegram_msg = (
                f"\u274c *ORDER REJECTED*\n\n"
                f"\U0001f4c8 *Symbol:* {symbol}\n"
                f"\U0001f4e6 *Quantity:* {quantity}\n"
                f"\U0001f4b0 *Entry Price:* \u20b9{entry_price:,.2f}\n"
                f"\U0001f4b8 *Order Value:* \u20b9{order_value:,.0f}\n"
                f"\u26a0\ufe0f *Reason:* {validation['reason']}{shortfall_msg}"
            )
            if validation['balance_info']:
                telegram_msg += (
                    f"\n\n\U0001f4ca *Account Status:*\n"
                    f"\u20b9 Margin Available: \u20b9{validation['balance_info']['margin_available']:,.0f}\n"
                    f"\u20b9 Margin Required: \u20b9{order_value:,.0f}"
                )
            
            send_telegram_notification(telegram_msg)
            
            # Return validation error (do NOT place order)
            return jsonify({
                'success': False,
                'error': validation['reason'],
                'validation_failed': True,
                'shortfall': validation['shortfall'],
                'balance_info': validation['balance_info'],
                'order_value': order_value
            }), 402  # 402 Payment Required
        
        logger.info(f"? Funds validated successfully for {symbol}")
        # =====================================================================
        
        logger.info("Session obtained, searching for symbol...")
        # Search for symbol
        try:
            search_result = smart.searchScrip("NSE", symbol)
            logger.info(f"Search result received: {len(search_result.get('data', []))} results")
        except Exception as e:
            logger.error(f"Symbol search failed: {e}", exc_info=True)
            return jsonify({'success': False, 'error': f'Symbol search failed: {e}'}), 500

        if not search_result.get('data'):
            logger.warning(f"Order placement failed: Symbol {symbol} not found")
            return jsonify({'success': False, 'error': f'Symbol {symbol} not found'}), 404

        # Find exact match — prefer symbol with valid (non-empty) symboltoken
        scrip_data = None
        # 1st pass: exact tradingsymbol match with valid token
        for result in search_result['data']:
            if result.get('tradingsymbol') == symbol + '-EQ' and result.get('symboltoken'):
                scrip_data = result
                break
        # 2nd pass: any result with valid token
        if not scrip_data:
            for result in search_result['data']:
                if result.get('symboltoken'):
                    scrip_data = result
                    break
        # 3rd pass: fallback to first result regardless
        if not scrip_data:
            scrip_data = search_result['data'][0]

        trading_symbol = scrip_data.get('tradingsymbol')
        symbol_token   = scrip_data.get('symboltoken', '')

        # If token is still empty, Angel One can't place this order
        if not symbol_token:
            logger.error(f"Order failed: no symboltoken found for {symbol}. "
                         f"Angel One search returned: {[r.get('tradingsymbol') for r in search_result['data']]}")
            return jsonify({
                'success': False,
                'error': f'Symbol {symbol} not found on Angel One (no token). '
                         f'It may be delisted or use a different symbol name.'
            }), 400

        logger.info(f"Found trading symbol: {trading_symbol}, token: {symbol_token}")
        
        # Place limit order
        order_params = {
            "variety": "NORMAL",
            "tradingsymbol": trading_symbol,
            "symboltoken": symbol_token,
            "transactiontype": "BUY",
            "exchange": "NSE",
            "ordertype": "LIMIT",
            "producttype": "DELIVERY",
            "duration": "DAY",
            "price": str(round(entry_price)),
            "quantity": str(quantity),
            "squareoff": "0",
            "stoploss": "0",
            "trailingstoploss": "0"
        }
        
        logger.info(f"Sending order to Angel One with params: {order_params}")
        try:
            result = smart.placeOrder(order_params)
            logger.info(f"Order result received: {result}")
        except Exception as e:
            err_str = str(e).lower()
            if 'cautionary' in err_str or 'surveillance' in err_str:
                logger.error(f"Order rejected (cautionary listing): {symbol} — {e}")
                return jsonify({'success': False, 'error': f'{symbol} is under NSE surveillance/cautionary listing — place manually in Angel One app'}), 400
            logger.error(f"Place order API call failed: {e}", exc_info=True)
            return jsonify({'success': False, 'error': f'Place order failed: {e}'}), 500
        
        if isinstance(result, str):
            order_id = result
        elif isinstance(result, dict) and result.get('status'):
            order_id = result.get('data', result.get('orderid'))
        else:
            # Extract meaningful error from Angel One response
            error_msg = 'Order placement failed'
            if isinstance(result, dict):
                api_msg = result.get('message', '') or result.get('errorMessage', '') or ''
                if 'cautionary' in api_msg.lower() or 'surveillance' in api_msg.lower():
                    error_msg = f'{symbol} is under NSE surveillance/cautionary listing — place manually in Angel One app'
                elif api_msg:
                    error_msg = api_msg
            logger.error(f"Order placement failed for {symbol}: {result}")
            return jsonify({'success': False, 'error': error_msg}), 400
        
        logger.info(f"Order placed successfully: {order_id}")
        
        # Send Telegram notification ONLY AFTER order is confirmed
        trade_value = entry_price * quantity
        source = data.get('source', 'Dashboard')
        message = (
            f"\u2705 *ORDER PLACED*\n\n"
            f"\U0001f4c8 *Symbol:* {symbol}\n"
            f"\U0001f4e6 *Quantity:* {quantity} shares\n"
            f"\U0001f4b0 *Entry Price:* \u20b9{entry_price:,.2f}\n"
            f"\U0001f3af *Target:* \u20b9{target_price:,.2f}\n"
            f"\U0001f6d1 *Stop Loss:* \u20b9{stop_loss:,.2f}\n"
            f"\U0001f4b8 *Order Value:* \u20b9{trade_value:,.0f}\n"
            f"\U0001f9fe *Order ID:* `{order_id}`\n"
            f"\U0001f4cd *Source:* {source}\n\n"
            f"Position is now being tracked in Radar tab."
        )
        send_telegram_notification(message)
        
        # Save order to file
        order_record = {
            'order_id': order_id,
            'symbol': symbol,
            'quantity': quantity,
            'entry_price': entry_price,
            'target_price': target_price,
            'stop_loss': stop_loss,
            'placed_at': datetime.now().isoformat(),
            'status': 'PENDING'
        }
        
        # Append to orders log
        orders_log = []
        if os.path.exists('angel_orders.json'):
            try:
                with open('angel_orders.json') as f:
                    orders_log = json.load(f)
                    if not isinstance(orders_log, list):
                        orders_log = []
            except Exception as e:
                logger.warning(f"Could not read existing orders: {e}")
                orders_log = []
        
        orders_log.append(order_record)
        
        with open('angel_orders.json', 'w') as f:
            json.dump(orders_log, f, indent=2)
        
        logger.info(f"Order logged to angel_orders.json")
        
        return jsonify({
            'success': True,
            'order_id': order_id,
            'symbol': symbol,
            'quantity': quantity,
            'message': f'Order placed successfully! Order ID: {order_id}'
        }), 200
        
    except Exception as e:
        logger.error(f"Order placement error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/order-status/<order_id>', methods=['GET'])
def order_status(order_id):
    """Get order status from Angel One"""
    if not check_api_key():
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    try:
        smart = get_angel_session()
        if not smart:
            logger.error("Order status query failed: Could not connect to Angel One")
            return jsonify({'success': False, 'error': 'Failed to connect to Angel One'}), 401
        
        orders = smart.orderBook()
        if not isinstance(orders, dict) or not orders.get('data'):
            logger.warning(f"Order status query: No orders found for {order_id}")
            return jsonify({'success': False, 'error': 'No orders found'}), 404
        
        for order in orders['data']:
            if order.get('orderid') == order_id:
                logger.info(f"Order status found: {order_id} - {order.get('status')}")
                return jsonify({
                    'success': True,
                    'order': {
                        'order_id': order.get('orderid'),
                        'symbol': order.get('tradingsymbol'),
                        'status': order.get('status'),
                        'quantity': order.get('quantity'),
                        'filled': order.get('filledshares'),
                        'price': order.get('price')
                    }
                }), 200
        
        logger.warning(f"Order not found: {order_id}")
        return jsonify({'success': False, 'error': 'Order not found'}), 404
        
    except Exception as e:
        logger.error(f"Order status error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/get-quote', methods=['GET'])
def get_quote():
    """Get live LTP (Last Traded Price) from Angel One for a symbol"""
    try:
        symbol = request.args.get('symbol', '').strip()
        
        if not symbol:
            logger.warning("Quote request: No symbol provided")
            return jsonify({'success': False, 'error': 'No symbol provided'}), 400
        
        logger.info(f"Fetching live quote for: {symbol}")
        
        smart = get_angel_session()
        if not smart:
            logger.error(f"Quote fetch failed for {symbol}: Could not connect to Angel One")
            # Fallback to yfinance when Angel One session unavailable
            try:
                import yfinance as yf
                clean_sym = symbol.replace('-EQ', '').replace('.NS', '').replace('.BO', '').strip().upper()
                yf_symbol = clean_sym + '.NS'
                ticker_obj = yf.Ticker(yf_symbol)
                # Use 1m interval to get latest intraday traded price (NOT daily EOD close)
                hist_1m = ticker_obj.history(period='1d', interval='1m')
                # Prev close from daily data for Day P&L calculation
                hist_1d = ticker_obj.history(period='2d', interval='1d')
                prev_close = float(hist_1d['Close'].iloc[-2]) if len(hist_1d) >= 2 else 0
                if not hist_1m.empty:
                    last = hist_1m.dropna(subset=['Close']).iloc[-1]
                    ltp = float(last['Close'])
                    logger.info(f"yfinance fallback (no session) for {symbol}: LTP={ltp}, PrevClose={prev_close}")
                    return jsonify({
                        'success': True,
                        'symbol': symbol,
                        'ltp': ltp,
                        'open': float(hist_1m['Open'].iloc[0]) if not hist_1m.empty else 0,
                        'high': float(hist_1m['High'].max()),
                        'low': float(hist_1m['Low'].min()),
                        'close': prev_close,   # yesterday's close for Day P&L
                        'volume': int(hist_1m['Volume'].sum()) if 'Volume' in hist_1m.columns else 0,
                        'timestamp': datetime.now().isoformat(),
                        'source': 'yfinance'
                    }), 200
            except Exception as yf_e:
                logger.warning(f"yfinance fallback also failed for {symbol}: {yf_e}")
            return jsonify({'success': False, 'error': 'Failed to connect to Angel One'}), 401
        
        # Add .NS suffix if not present
        if not symbol.endswith('-EQ'):
            quote_symbol = symbol + '-EQ'
        else:
            quote_symbol = symbol
        
        # Fetch quote from Angel One using ltpData
        try:
            quote_data = smart.ltpData("NSE", quote_symbol, "")
            logger.info(f"Quote response for {symbol}: {quote_data}")
            
            if isinstance(quote_data, dict) and quote_data.get('status'):
                data = quote_data.get('data', {})
                if isinstance(data, list) and len(data) > 0:
                    data = data[0]
                
                ltp = float(data.get('ltp', 0))
                
                if ltp > 0:
                    close_val = float(data.get('close', 0))
                    # ltpData often returns close=0 during market hours.
                    # Fall back to yfinance for prev-day close (used for Day P&L calculation).
                    if close_val <= 0:
                        try:
                            import yfinance as yf
                            import pandas as pd
                            from datetime import datetime
                            clean_sym = symbol.replace('-EQ', '').replace('.NS', '').strip().upper()
                            hist_1d = yf.Ticker(clean_sym + '.NS').history(period='5d', interval='1d')
                            if not hist_1d.empty:
                                # Filter out today's partial data to ensure we get yesterday's close
                                today = datetime.now().date()
                                hist_1d = hist_1d[hist_1d.index.date < today]
                                if len(hist_1d) >= 1:
                                    close_val = round(float(hist_1d['Close'].iloc[-1]), 2)
                                    logger.info(f"Enriched prev-close for {symbol} from yfinance: {close_val}")
                        except Exception as e:
                            logger.warning(f"Failed to fetch prev-close for {symbol}: {e}")
                            pass  # Day P&L will show 0 — acceptable fallback
                    logger.info(f"Quote fetched for {symbol}: LTP={ltp}, PrevClose={close_val}")
                    return jsonify({
                        'success': True,
                        'symbol': symbol,
                        'ltp': ltp,
                        'open': float(data.get('open', 0)),
                        'high': float(data.get('high', 0)),
                        'low': float(data.get('low', 0)),
                        'close': close_val,
                        'volume': int(data.get('tradeVolume', data.get('volume', 0))),
                        'timestamp': datetime.now().isoformat()
                    }), 200
            
            logger.warning(f"Angel One ltpData returned no LTP for {symbol}: {quote_data} — trying yfinance")

        except Exception as e:
            logger.error(f"ltpData API failed for {symbol}: {e}", exc_info=True)

        # Fallback to yfinance (covers both invalid Angel One response AND exception)
        try:
            import yfinance as yf
            import pandas as pd
            from datetime import datetime
            # Normalize: strip any existing .NS / -EQ suffix before adding .NS
            clean_sym = symbol.replace('-EQ', '').replace('.NS', '').replace('.BO', '').strip().upper()
            yf_symbol = clean_sym + '.NS'
            ticker_obj = yf.Ticker(yf_symbol)
            # 1m interval = latest intraday price. Fall back to 1d if market closed/weekend
            hist_1m = ticker_obj.history(period='1d', interval='1m')
            hist_1d = ticker_obj.history(period='5d', interval='1d')
            
            # Calculate prev_close: filter out today to ensure we get yesterday's close
            today = datetime.now().date()
            hist_1d_filtered = hist_1d[hist_1d.index.date < today]
            prev_close = float(hist_1d_filtered['Close'].iloc[-1]) if len(hist_1d_filtered) >= 1 else 0
            
            if not hist_1m.empty:
                last = hist_1m.dropna(subset=['Close']).iloc[-1]
                ltp_yf = float(last['Close'])
                if ltp_yf > 0:
                    logger.info(f"yfinance 1m fallback for {symbol}: LTP={ltp_yf}, PrevClose={prev_close}")
                    return jsonify({
                        'success': True,
                        'symbol': symbol,
                        'ltp': ltp_yf,
                        'open': float(hist_1m['Open'].iloc[0]),
                        'high': float(hist_1m['High'].max()),
                        'low': float(hist_1m['Low'].min()),
                        'close': prev_close,
                        'volume': int(hist_1m['Volume'].sum()) if 'Volume' in hist_1m.columns else 0,
                        'timestamp': datetime.now().isoformat(),
                        'source': 'yfinance_1m'
                    }), 200
            # Outside market hours: fall back to last daily close
            if not hist_1d.empty:
                ltp_yf = float(hist_1d['Close'].iloc[-1])
                if ltp_yf > 0:
                    logger.info(f"yfinance 1d fallback for {symbol}: LTP={ltp_yf}, PrevClose={prev_close}")
                    return jsonify({
                        'success': True,
                        'symbol': symbol,
                        'ltp': ltp_yf,
                        'open': float(hist_1d['Open'].iloc[-1]),
                        'high': float(hist_1d['High'].iloc[-1]),
                        'low': float(hist_1d['Low'].iloc[-1]),
                        'close': prev_close,
                        'volume': int(hist_1d['Volume'].iloc[-1]) if 'Volume' in hist_1d.columns else 0,
                        'timestamp': datetime.now().isoformat(),
                        'source': 'yfinance_1d'
                    }), 200
        except Exception as yf_e:
            logger.warning(f"yfinance fallback failed for {symbol}: {yf_e}")

        return jsonify({'success': False, 'error': f'All quote sources failed for {symbol}'}), 503
    
    except Exception as e:
        logger.error(f"Quote endpoint error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/get-52w', methods=['GET'])
def get_52w():
    """Get ATH + 2020 COVID low using unadjusted NSE prices (matches Screener.in/TradingView)."""
    try:
        symbol = request.args.get('symbol', '').strip()
        if not symbol:
            return jsonify({'success': False, 'error': 'No symbol provided'}), 400

        logger.info(f"Fetching ATH + 2020 low (unadjusted) for: {symbol}")

        import yfinance as yf
        yf_symbol = symbol.upper().replace('-EQ', '') + '.NS'
        ticker_obj = yf.Ticker(yf_symbol)

        # Use unadjusted prices — matches NSE bhavcopy / Screener.in / TradingView
        hist = ticker_obj.history(period='max', interval='1d', auto_adjust=False)

        if hist.empty:
            return jsonify({'success': False, 'error': f'No data found for {symbol}'}), 404

        # ATH: all-time high from unadjusted daily data (post-2020)
        post_mask = hist.index >= '2020-01-01'
        ath = round(float(hist.loc[post_mask, 'High'].max()), 2) if post_mask.any() else round(float(hist['High'].max()), 2)

        # 2020 COVID low: full calendar year 2020 minimum (unadjusted — gives true structural low)
        year_mask = (hist.index.year == 2020)
        low_2020 = round(float(hist.loc[year_mask, 'Low'].min()), 2) if year_mask.any() else round(float(hist['Low'].min()), 2)

        # 52W for backward compat (also unadjusted)
        hist_1y = hist[hist.index >= (hist.index[-1] - pd.Timedelta(days=365))]
        week_52_high = round(float(hist_1y['High'].max()), 2) if not hist_1y.empty else ath
        week_52_low  = round(float(hist_1y['Low'].min()), 2)  if not hist_1y.empty else low_2020

        logger.info(f"{symbol}: ATH={ath}, 2020_low={low_2020}")
        return jsonify({
            'success':      True,
            'symbol':       symbol.upper().replace('-EQ', ''),
            'ath':          ath,
            'low_2020':     low_2020,
            'week_52_high': week_52_high,
            'week_52_low':  week_52_low,
        }), 200

    except Exception as e:
        logger.error(f"get-52w error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/sync-trades', methods=['GET'])
def sync_trades():
    """Fetch open holdings from Angel One and return them for Radar tab sync.
    
    Also fetches today's completed positions (buy+sell) so the frontend can
    record accurate entry AND exit prices when a position is auto-closed.
    
    Returns:
      open_trades    — positions currently held (from holding())
      closed_trades  — positions sold today with actual entry+exit price (from position())
      sell_exit_map  — {TICKER: exit_price} from order book for quick lookup
    """
    def _build_sell_exit_map(angel_orders):
        """
        Build {TICKER: avg_exit_price} from completed SELL orders today.
        If the same ticker was sold in multiple lots, averages the fill prices.
        """
        from collections import defaultdict
        sell_map = defaultdict(lambda: {'total_value': 0.0, 'total_qty': 0})
        for o in (angel_orders or []):
            if (str(o.get('transactiontype', '')).upper() != 'SELL'):
                continue
            if str(o.get('status', '')).upper() not in ('COMPLETE', 'FILLED', 'TRADED'):
                continue
            ticker = (o.get('tradingsymbol') or '').replace('-EQ', '').strip().upper()
            if not ticker:
                continue
            avg_price = float(o.get('averageprice') or o.get('averagePrice') or 0)
            filled    = int(o.get('filledshares') or o.get('filled_shares') or
                           o.get('qty') or o.get('quantity') or 0)
            if avg_price > 0 and filled > 0:
                sell_map[ticker]['total_value'] += avg_price * filled
                sell_map[ticker]['total_qty']   += filled
                logger.info(f"  SELL order: {ticker} qty={filled} @ ₹{avg_price}")

        # Compute weighted average exit price per ticker
        result = {}
        for ticker, data in sell_map.items():
            if data['total_qty'] > 0:
                result[ticker] = round(data['total_value'] / data['total_qty'], 2)
        return result

    def _do_sync(smart):
        """Inner sync — returns (open_trades, sell_exit_map, errors)."""
        open_trades   = []
        sell_exit_map = {}
        errors        = []

        # ── Holdings (positions currently held) ──────────────────────────
        try:
            holdings_response = smart.holding()
            logger.info(f"Holdings response type: {type(holdings_response)}")
            angel_positions = []
            if isinstance(holdings_response, dict) and holdings_response.get('status'):
                angel_positions = holdings_response.get('data', []) or []
                if not isinstance(angel_positions, list):
                    angel_positions = []
            elif isinstance(holdings_response, list):
                angel_positions = holdings_response
            logger.info(f"Found {len(angel_positions)} holdings in Angel One")

            for p in angel_positions:
                qty = int(p.get('quantity', p.get('t1quantity', 0)) or 0)
                if qty <= 0:
                    continue
                ticker = (p.get('tradingsymbol') or p.get('symbolname') or '').replace('-EQ', '').strip()
                if not ticker:
                    continue
                avg_price = float(p.get('averageprice') or p.get('averagePrice') or p.get('buyavgprice') or 0)
                ltp       = float(p.get('ltp') or p.get('lastprice') or p.get('close') or avg_price or 0)
                open_trades.append({
                    'ticker':        ticker,
                    'source':        'Angel One',
                    'quantity':      qty,
                    'entry_price':   round(avg_price, 2),
                    'current_price': round(ltp, 2),
                    'status':        'Open',
                    'triggered_at':  datetime.now().isoformat(),
                })
        except Exception as e:
            err = f"holding() failed: {e}"
            logger.error(err, exc_info=True)
            errors.append(err)

        # ── Order book — fetch SELL exits AND open orders ─────────────────
        try:
            orders_response = smart.orderBook()
            angel_orders = []
            if isinstance(orders_response, dict) and orders_response.get('status'):
                angel_orders = orders_response.get('data', []) or []
                if not isinstance(angel_orders, list):
                    angel_orders = []
            logger.info(f"Found {len(angel_orders)} orders in order book")

            # Build sell exit price map from completed SELL orders
            sell_exit_map = _build_sell_exit_map(angel_orders)
            if sell_exit_map:
                logger.info(f"Sell exit prices found for: {list(sell_exit_map.keys())}")
        except Exception as e:
            logger.warning(f"orderBook() failed (non-fatal): {e}")
            errors.append(f"orderBook() failed: {e}")

        # ── Position book — today's closed positions with BUY+SELL avg ───
        # Returns both entry (netbuyavgprice) and exit (netsellprice) for
        # stocks fully closed today. This is what Angel One's Positions tab shows.
        closed_from_positions = {}
        try:
            position_response = smart.position()
            pos_data = []
            if isinstance(position_response, dict) and position_response.get('status'):
                pos_data = position_response.get('data', []) or []
                if not isinstance(pos_data, list):
                    pos_data = []
            logger.info(f"Found {len(pos_data)} entries in position book")

            open_tickers = {t['ticker'].upper() for t in open_trades}
            for pos in pos_data:
                ticker = (pos.get('tradingsymbol') or '').replace('-EQ', '').strip().upper()
                if not ticker or ticker in open_tickers:
                    continue
                net_qty  = int(pos.get('netqty', 0) or 0)
                # Only fully closed positions (net qty = 0)
                if net_qty != 0:
                    continue
                buy_avg  = float(pos.get('netbuyavgprice') or pos.get('buyavgprice') or 0)
                sell_avg = float(pos.get('netsellprice') or pos.get('sellavgprice') or 0)
                qty      = int(pos.get('netbuyqty') or pos.get('buyqty') or 0)
                if buy_avg > 0 and sell_avg > 0:
                    # Position data overrides order book (more accurate — includes avg buy price)
                    sell_exit_map[ticker] = round(sell_avg, 2)
                    closed_from_positions[ticker] = {
                        'ticker':      ticker,
                        'entry_price': round(buy_avg, 2),
                        'exit_price':  round(sell_avg, 2),
                        'quantity':    qty,
                        'source':      'Angel One',
                        'status':      'Closed',
                        'exit_source': 'Angel One positions',
                        'closed_at':   datetime.now().isoformat(),
                    }
                    logger.info(f"Position closed today: {ticker} "
                                f"buy=₹{buy_avg} sell=₹{sell_avg} qty={qty}")
        except Exception as e:
            logger.warning(f"position() failed (non-fatal): {e}")
            errors.append(f"position() failed: {e}")

        return open_trades, sell_exit_map, closed_from_positions, errors

    try:
        logger.info("Starting trade sync with Angel One...")

        smart = get_angel_session()
        if not smart:
            logger.error("Trade sync failed: Could not connect to Angel One")
            return jsonify({'success': False, 'error': 'Failed to connect to Angel One'}), 401

        open_trades, sell_exit_map, closed_from_positions, errors = _do_sync(smart)

        # Retry with fresh session if first attempt failed
        if not open_trades and errors:
            logger.warning("First sync attempt returned errors — retrying with fresh session")
            smart = get_angel_session(force_refresh=True)
            if smart:
                open_trades, sell_exit_map, closed_from_positions, errors = _do_sync(smart)
            else:
                return jsonify({'success': False, 'error': 'Session refresh failed'}), 401

        # ── Build closed_trades list — prefer position() data (has entry+exit+qty) ──
        # Fall back to sell_exit_map (order book, exit only) for anything not in positions
        open_tickers = {t['ticker'].upper() for t in open_trades}
        closed_trades_list = []
        seen = set()

        # First: add position-based closed trades (most complete data)
        for ticker, pos_data in closed_from_positions.items():
            if ticker not in open_tickers:
                closed_trades_list.append(pos_data)
                seen.add(ticker)

        # Then: add order-book-only exits for tickers not in positions
        for ticker, exit_price in sell_exit_map.items():
            if ticker not in open_tickers and ticker not in seen:
                closed_trades_list.append({
                    'ticker':     ticker,
                    'exit_price': exit_price,
                    'source':     'Angel One',
                    'closed_at':  datetime.now().isoformat(),
                    'status':     'Closed',
                })
                logger.info(f"Auto-closed: {ticker} exit=₹{exit_price}")

        # ── Update local radar_trades.json ────────────────────────────────
        try:
            radar_trades = []
            try:
                with open('radar_trades.json') as f:
                    radar_trades = json.load(f)
                    if not isinstance(radar_trades, list):
                        radar_trades = []
            except Exception:
                radar_trades = []

            live_tickers = {t['ticker'] for t in open_trades}
            updated_radar = [t for t in radar_trades if t.get('ticker') in live_tickers]

            with open('radar_trades.json', 'w') as f:
                json.dump(updated_radar, f, indent=2)
            logger.info(f"Updated radar_trades.json with {len(updated_radar)} open trades")
            
            # ── Update DynamoDB with synced open trades ────────────────────
            try:
                from dynamodb_helper import get_helper
                dh = get_helper()
                if dh:
                    dh.write_all_trades(open_trades)
                    logger.info(f"✅ DynamoDB updated with {len(open_trades)} open trades")
            except Exception as db_err:
                logger.warning(f"DynamoDB update failed (non-fatal): {db_err}")
        except Exception as e:
            logger.warning(f"radar_trades.json update failed (non-fatal): {e}")

        return jsonify({
            'success':         True,
            'open_trades':     open_trades,
            'closed_trades':   closed_trades_list,   # ← actual exit prices from order book
            'sell_exit_map':   sell_exit_map,         # ← {TICKER: exit_price} for direct lookup
            'radar_trades':    len(open_trades),
            'total_positions': len(open_trades),
            'message':         f'Synced {len(open_trades)} open, {len(closed_trades_list)} closed today',
            'warnings':        errors if errors else [],
        }), 200

    except Exception as e:
        logger.error(f"Trade sync error: {e}", exc_info=True)
        global _angel_session, _angel_session_expiry
        _angel_session = None
        _angel_session_expiry = 0
        return jsonify({'success': False, 'error': str(e)}), 500


# ─── GROWW PORTFOLIO SYNC ──────────────────────────────────────────────────────
# Read-only: fetches open holdings from Groww and returns them in the same
# format as /api/sync-trades so the Radar tab can merge both brokers.
#
# Auth: GROWW_USER_API_KEY is the daily access token issued by Groww.
# It is used directly as a Bearer token — no exchange needed.
# Update GROWW_USER_API_KEY in the systemd override each day.

GROWW_USER_API_KEY = os.environ.get('GROWW_USER_API_KEY', '')

# Angel One session cache — reuse session for 4 hours to avoid rate limits
_angel_session = None
_angel_session_expiry = 0

def get_groww_access_token():
    """Return the Groww access token directly from env var."""
    if not GROWW_USER_API_KEY:
        logger.error("Groww: GROWW_USER_API_KEY not set in environment")
        return None
    logger.info("Groww: using access token from GROWW_USER_API_KEY env var")
    return GROWW_USER_API_KEY

@app.route('/api/save-radar', methods=['POST', 'OPTIONS'])
def save_radar():
    """Save radar trades state — accepts POST from frontend."""
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    try:
        data = request.get_json(force=True, silent=True) or {}
        logger.info(f"save-radar received {len(data)} fields")
        return jsonify({'success': True, 'message': 'Radar state received'}), 200
    except Exception as e:
        logger.error(f"save-radar error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ── DynamoDB-backed endpoints ─────────────────────────────────────────────────
# These replace the old pattern of fetching raw JSON from GitHub Pages CDN.
# The dashboard calls these instead, getting faster + always-fresh data.

def _get_dynamo_helper():
    """Lazy import dynamodb_helper so the app starts even without boto3."""
    try:
        import dynamodb_helper as dh
        return dh
    except ImportError:
        logger.warning("dynamodb_helper not available — boto3 may not be installed")
        return None


@app.route('/api/signals', methods=['GET'])
def get_signals():
    """
    Return stock signals with live LTPs merged in — single API call.
    Query params:
      type = TRENDLINE | VOLUME | GOLDEN | MICROCAP250  (default: TRENDLINE + VOLUME)
    Response: { success, source, data: { trendline: [...], volume: [...], microcap250: [...] } }
    Each signal has ltp field merged from LivePrices table.
    Falls back to local JSON files if DynamoDB unavailable.
    """
    try:
        signal_type = request.args.get('type', '').upper()
        dh = _get_dynamo_helper()

        result = {}
        source = 'dynamodb'

        if dh:
            types = [signal_type] if signal_type in ('TRENDLINE', 'VOLUME', 'GOLDEN', 'MICROCAP250') else ['TRENDLINE', 'VOLUME']
            for st in types:
                result[st.lower()] = dh.read_signals(st)

            # Merge live LTPs from LivePrices table into signals
            try:
                all_tickers = []
                for signals in result.values():
                    for s in signals:
                        t = (s.get('ticker') or s.get('symbol') or '').upper()
                        if t:
                            all_tickers.append(t)
                if all_tickers:
                    prices = dh.read_prices(all_tickers)
                    for signals in result.values():
                        for s in signals:
                            t = (s.get('ticker') or s.get('symbol') or '').upper()
                            if t in prices:
                                s['ltp'] = prices[t]
            except Exception as e:
                logger.warning(f"LTP merge failed (signals still returned): {e}")

            logger.info(f"DynamoDB signals: {[(k, len(v)) for k, v in result.items()]}")
        else:
            # Fallback: read local JSON files
            logger.warning("DynamoDB unavailable — falling back to JSON files")
            source = 'json_fallback'
            for fname, key in [
                ('trendline_screen.json', 'trendline'),
                ('microcap250_screen.json', 'microcap250'),
                ('volume_gainer_watchlist.json', 'volume'),
            ]:
                paths = [fname, f'/home/ubuntu/stock-yard-backend/{fname}', f'/home/ubuntu/{fname}']
                for p in paths:
                    if os.path.exists(p):
                        with open(p) as f:
                            raw = json.load(f)
                        result[key] = raw if isinstance(raw, list) else raw.get('volume_gainer_stocks', raw.get('stocks', []))
                        break

        return jsonify({'success': True, 'source': source, 'data': result}), 200

    except Exception as e:
        logger.error(f"get_signals error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


# ── Manual Stocks ─────────────────────────────────────────────────────────────
MANUAL_TABLE = 'ManualStocks'

@app.route('/api/manual-stocks', methods=['GET', 'POST', 'DELETE', 'OPTIONS'])
def manual_stocks():
    """
    GET    /api/manual-stocks          — return all manual stocks
    POST   /api/manual-stocks          — save/upsert a manual stock (body: stock dict)
    DELETE /api/manual-stocks?symbol=X — delete a manual stock by symbol
    """
    if request.method == 'OPTIONS':
        return jsonify({'success': True}), 200
    try:
        import boto3
        from decimal import Decimal

        AWS_REGION = os.environ.get('AWS_REGION', 'us-east-1')
        db    = boto3.resource('dynamodb', region_name=AWS_REGION)
        table = db.Table(MANUAL_TABLE)

        def _dec(obj):
            if isinstance(obj, float): return Decimal(str(round(obj, 6)))
            if isinstance(obj, dict):  return {k: _dec(v) for k, v in obj.items()}
            if isinstance(obj, list):  return [_dec(i) for i in obj]
            return obj

        def _flt(obj):
            from decimal import Decimal as D
            if isinstance(obj, D): return float(obj)
            if isinstance(obj, dict): return {k: _flt(v) for k, v in obj.items()}
            if isinstance(obj, list): return [_flt(i) for i in obj]
            return obj

        if request.method == 'GET':
            resp = table.scan()
            items = _flt(resp.get('Items', []))
            items.sort(key=lambda x: x.get('addedAt',''), reverse=True)
            return jsonify({'success': True, 'stocks': items}), 200

        elif request.method == 'POST':
            stock = request.json
            if not stock or not stock.get('symbol'):
                return jsonify({'success': False, 'error': 'symbol required'}), 400
            item = _dec(stock)
            item['symbol']    = stock['symbol'].upper()
            item['updatedAt'] = datetime.utcnow().isoformat()
            table.put_item(Item=item)
            logger.info(f"Manual stock saved: {stock['symbol']}")
            return jsonify({'success': True}), 200

        elif request.method == 'DELETE':
            symbol = request.args.get('symbol', '').upper()
            if not symbol:
                return jsonify({'success': False, 'error': 'symbol required'}), 400
            table.delete_item(Key={'symbol': symbol})
            logger.info(f"Manual stock deleted: {symbol}")
            return jsonify({'success': True}), 200

    except Exception as e:
        logger.error(f"manual_stocks error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/radar', methods=['GET', 'POST', 'OPTIONS'])
def radar_endpoint():
    """
    GET  /api/radar               — return all trades from DynamoDB
    GET  /api/radar?status=Open   — filter by status
    POST /api/radar               — write a single trade to DynamoDB
    """
    if request.method == 'OPTIONS':
        return jsonify({}), 200

    dh = _get_dynamo_helper()

    if request.method == 'GET':
        try:
            status_filter = request.args.get('status')
            if dh:
                trades = dh.read_trades(status_filter)
                logger.info(f"DynamoDB radar served: {len(trades)} trades")
                return jsonify({'success': True, 'source': 'dynamodb', 'trades': trades}), 200

            # Fallback to JSON
            radar_file = '/home/ubuntu/radar_trades.json'
            if not os.path.exists(radar_file):
                radar_file = 'radar_trades.json'
            if os.path.exists(radar_file):
                with open(radar_file) as f:
                    trades = json.load(f)
                if status_filter:
                    trades = [t for t in trades if t.get('status') == status_filter]
                return jsonify({'success': True, 'source': 'json_fallback', 'trades': trades}), 200
            return jsonify({'success': True, 'source': 'empty', 'trades': []}), 200

        except Exception as e:
            logger.error(f"radar GET error: {e}", exc_info=True)
            return jsonify({'success': False, 'error': str(e)}), 500

    if request.method == 'POST':
        try:
            trade = request.get_json(force=True) or {}
            if not trade.get('ticker'):
                return jsonify({'success': False, 'error': 'ticker required'}), 400
            if dh:
                trade_id = dh.write_trade(trade)
                logger.info(f"DynamoDB: trade written {trade.get('ticker')} id={trade_id}")
                return jsonify({'success': True, 'trade_id': trade_id}), 200
            return jsonify({'success': False, 'error': 'DynamoDB not available'}), 503
        except Exception as e:
            logger.error(f"radar POST error: {e}", exc_info=True)
            return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/prices', methods=['GET'])
def get_prices():
    """
    GET /api/prices                           — return all cached LTPs
    GET /api/prices?tickers=INFY,TCS,SBIN    — return specific tickers
    
    Strategy:
    1. Try Angel One live LTP (most accurate, real-time)
    2. Fall back to DynamoDB cache if Angel One fails
    3. Fall back to yfinance batch if DynamoDB is stale
    """
    try:
        tickers_param = request.args.get('tickers', '')
        tickers = [t.strip().upper() for t in tickers_param.split(',') if t.strip()] if tickers_param else []

        if not tickers:
            # No tickers specified — return from DynamoDB only
            dh = _get_dynamo_helper()
            if dh:
                prices = dh.read_prices(None)
                return jsonify({'success': True, 'source': 'dynamodb', 'prices': prices}), 200
            return jsonify({'success': True, 'source': 'empty', 'prices': {}}), 200

        prices = {}

        # ── Primary: yfinance batch (fast, live, no token needed) ────────
        # yfinance 1m intraday data is 15-min delayed but acceptable for dashboard
        # This is the most reliable bulk source — works always, no auth needed
        try:
            import yfinance as yf
            symbols_ns = [t + '.NS' for t in tickers]
            data_yf = yf.download(symbols_ns, period='1d', interval='1m',
                                  auto_adjust=True, progress=False, threads=True)
            if not data_yf.empty and 'Close' in data_yf.columns:
                last = data_yf['Close'].iloc[-1]
                for sym in symbols_ns:
                    # Multi-ticker: columns are (field, ticker), single: just field
                    try:
                        if len(symbols_ns) == 1:
                            val = float(data_yf['Close'].iloc[-1])
                        else:
                            val = float(last[sym]) if sym in last.index else 0
                        if val > 0:
                            prices[sym.replace('.NS', '')] = round(val, 2)
                    except Exception:
                        pass
            if len(prices) >= len(tickers) * 0.7:
                logger.info(f"yfinance 1m live prices: {len(prices)}/{len(tickers)}")
                # Cache in DynamoDB
                dh = _get_dynamo_helper()
                if dh and prices:
                    try:
                        dh.write_prices_bulk(prices)
                    except Exception:
                        pass
                return jsonify({'success': True, 'source': 'yfinance_live', 'prices': prices}), 200
        except Exception as e:
            logger.warning(f"yfinance 1m batch failed: {e}")

        # ── Secondary: Angel One live LTP (requires token lookup per ticker) ─
        try:
            smart = get_angel_session()
            if smart:
                for ticker in tickers:
                    if ticker in prices:
                        continue
                    try:
                        search = smart.searchScrip('NSE', ticker)
                        token = ''
                        if search and search.get('data'):
                            for item in search['data']:
                                if item.get('tradingsymbol') == ticker + '-EQ':
                                    token = item.get('symboltoken', '')
                                    break
                        if token:
                            quote_data = smart.ltpData('NSE', ticker + '-EQ', token)
                            if quote_data and quote_data.get('status'):
                                data = quote_data.get('data', {})
                                if isinstance(data, list) and data:
                                    data = data[0]
                                ltp = float(data.get('ltp', 0))
                                if ltp > 0:
                                    prices[ticker] = ltp
                    except Exception:
                        pass
                if len(prices) >= len(tickers) * 0.5:
                    logger.info(f"Angel One live prices: {len(prices)}/{len(tickers)}")
                    # Update DynamoDB cache with fresh prices
                    dh = _get_dynamo_helper()
                    if dh and prices:
                        try:
                            dh.write_prices_bulk(prices)
                        except Exception:
                            pass
                    return jsonify({'success': True, 'source': 'angel_one', 'prices': prices}), 200
        except Exception as e:
            logger.warning(f"Angel One bulk LTP failed: {e}")

        # ── Fallback 1: DynamoDB cache (with staleness check) ─────────────
        dh = _get_dynamo_helper()
        if dh:
            cached = dh.read_prices(tickers)
            if cached:
                logger.info(f"DynamoDB fallback: {len(cached)}/{len(tickers)} prices")
                # Merge: use cached for anything Angel One missed
                for t, p in cached.items():
                    if t not in prices:
                        prices[t] = p
                if len(prices) > 0:
                    return jsonify({'success': True, 'source': 'dynamodb', 'prices': prices}), 200
            logger.warning(f"DynamoDB returned no prices (all stale?) — falling back to yfinance")

        # ── Fallback 2: yfinance batch ────────────────────────────────────
        try:
            import yfinance as yf
            symbols = [t + '.NS' for t in tickers if t not in prices]
            if symbols:
                data = yf.download(symbols, period='2d', interval='1d',
                                   auto_adjust=True, progress=False, threads=True)
                if not data.empty and 'Close' in data:
                    last = data['Close'].iloc[-1]
                    for sym in symbols:
                        val = last.get(sym) if hasattr(last, 'get') else None
                        if val and float(val) > 0:
                            prices[sym.replace('.NS', '')] = round(float(val), 2)
            logger.info(f"yfinance fallback: {len(prices)}/{len(tickers)} prices")
        except Exception as e:
            logger.warning(f"yfinance batch fallback failed: {e}")

        if prices:
            return jsonify({'success': True, 'source': 'yfinance', 'prices': prices}), 200

        return jsonify({'success': True, 'source': 'dynamodb_stale', 'prices': {}}), 200

    except Exception as e:
        logger.error(f"get_prices error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/prices/update', methods=['POST'])
def update_prices():
    """
    POST /api/prices/update
    Body: { "prices": { "INFY": 1800.5, "TCS": 3400.0 } }
    Called by the live price updater cron to batch-write LTPs to DynamoDB.
    """
    try:
        body = request.get_json(force=True) or {}
        prices = body.get('prices', {})
        if not prices:
            return jsonify({'success': False, 'error': 'No prices provided'}), 400

        dh = _get_dynamo_helper()
        if dh:
            dh.write_prices_bulk(prices)
            logger.info(f"Bulk price update: {len(prices)} tickers")
            return jsonify({'success': True, 'updated': len(prices)}), 200

        return jsonify({'success': False, 'error': 'DynamoDB not available'}), 503

    except Exception as e:
        logger.error(f"update_prices error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/sync-groww', methods=['GET'])
def sync_groww():
    """
    Fetch open holdings from Groww (read-only).
    Returns positions in the same schema as /api/sync-trades.
    """
    try:
        access_token = get_groww_access_token()
        if not access_token:
            return jsonify({'success': False, 'error': 'Could not obtain Groww access token — check GROWW_USER_API_KEY and GROWW_API_SECRET env vars'}), 401

        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'X-API-VERSION': '1.0',
        }

        # 1. Fetch holdings (long-term DEMAT positions)
        # Real Groww response: {"status":"SUCCESS","payload":{"holdings":[...]}}
        holdings_resp = requests.get(
            'https://api.groww.in/v1/holdings/user',
            headers=headers, timeout=10
        )
        logger.info(f"Groww holdings status: {holdings_resp.status_code}")
        logger.info(f"Groww holdings raw response: {holdings_resp.text[:500]}")

        holdings = []
        if holdings_resp.status_code == 200:
            data = holdings_resp.json()
            if isinstance(data, list):
                holdings = data
            elif isinstance(data, dict):
                # Real API wraps under payload.holdings
                payload = data.get('payload', data)
                holdings = payload.get('holdings', payload.get('holdingsSummary', payload.get('data', [])))
            if not isinstance(holdings, list):
                holdings = []

        # 2. Fetch intraday/CNC positions
        # Real Groww response: {"status":"SUCCESS","payload":{"positions":[...]}}
        positions = []
        try:
            pos_resp = requests.get(
                'https://api.groww.in/v1/positions/user',  # correct endpoint
                headers=headers, timeout=10
            )
            logger.info(f"Groww positions status: {pos_resp.status_code}")
            logger.info(f"Groww positions raw response: {pos_resp.text[:500]}")
            if pos_resp.status_code == 200:
                pos_data = pos_resp.json()
                if isinstance(pos_data, list):
                    positions = pos_data
                elif isinstance(pos_data, dict):
                    payload = pos_data.get('payload', pos_data)
                    positions = payload.get('positions', payload.get('data', []))
                if not isinstance(positions, list):
                    positions = []
        except Exception as pe:
            logger.warning(f"Groww positions fetch failed: {pe}")

        logger.info(f"Groww: {len(holdings)} holdings, {len(positions)} intraday positions")

        # 3. Normalize to radar format
        # Real Groww field names: trading_symbol, quantity, average_price (snake_case)
        open_trades = []

        for h in holdings:
            symbol = (
                h.get('trading_symbol') or h.get('tradingSymbol') or
                h.get('symbol') or ''
            ).replace('-EQ', '').replace('.NS', '').strip()
            if not symbol:
                continue

            qty = int(h.get('quantity') or 0)
            if qty <= 0:
                continue

            avg_price = float(h.get('average_price') or h.get('averagePrice') or 0)
            ltp = float(h.get('ltp') or h.get('lastTradedPrice') or avg_price)

            open_trades.append({
                'ticker':        symbol,
                'source':        'Groww',
                'quantity':      qty,
                'entry_price':   round(avg_price, 2),
                'current_price': round(ltp, 2),
                'status':        'Open',
                'triggered_at':  datetime.now().isoformat(),
            })

        for p in positions:
            symbol = (
                p.get('trading_symbol') or p.get('tradingSymbol') or p.get('symbol') or ''
            ).replace('-EQ', '').replace('.NS', '').strip()
            if not symbol:
                continue

            qty = int(p.get('quantity') or p.get('net_carry_forward_quantity') or 0)
            if qty <= 0:
                continue

            # Skip if already added from holdings
            if any(t['ticker'] == symbol for t in open_trades):
                continue

            # Groww positions: average buy price is net_price or credit_price
            avg_price = float(p.get('net_price') or p.get('credit_price') or p.get('average_price') or 0)
            ltp = float(p.get('ltp') or p.get('lastTradedPrice') or avg_price)

            open_trades.append({
                'ticker':        symbol,
                'source':        'Groww',
                'quantity':      qty,
                'entry_price':   round(avg_price, 2),
                'current_price': round(ltp, 2),
                'status':        'Open',
                'triggered_at':  datetime.now().isoformat(),
            })

        logger.info(f"Groww sync returning {len(open_trades)} open positions")
        return jsonify({
            'success':    True,
            'source':     'Groww',
            'open_trades': open_trades,
            'count':      len(open_trades),
        }), 200

    except Exception as e:
        logger.error(f"Groww sync error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/set-groww-token', methods=['POST', 'OPTIONS'])
def set_groww_token():
    """
    Update GROWW_USER_API_KEY at runtime without SSH.
    POST body: { "token": "<bearer token>", "secret": "<admin secret>" }
    The admin secret must match ADMIN_SECRET env var on EC2 (set once, never changes).
    """
    if request.method == 'OPTIONS':
        return _cors_preflight()

    try:
        data = request.get_json(force=True) or {}
        token  = (data.get('token') or '').strip()
        secret = (data.get('secret') or '').strip()

        # Validate admin secret (set ADMIN_SECRET env var on EC2 once)
        admin_secret = os.environ.get('ADMIN_SECRET', '')
        if not admin_secret:
            return jsonify({'success': False, 'error': 'ADMIN_SECRET not configured on server'}), 500
        if secret != admin_secret:
            return jsonify({'success': False, 'error': 'Invalid admin secret'}), 403
        if not token:
            return jsonify({'success': False, 'error': 'No token provided'}), 400

        # Write token to systemd override file and restart service
        override_dir  = '/etc/systemd/system/angel-api.service.d'
        override_file = f'{override_dir}/groww.conf'
        content = f'[Service]\nEnvironment="GROWW_USER_API_KEY={token}"\n'

        import subprocess
        subprocess.run(['sudo', 'mkdir', '-p', override_dir], check=True)
        # Write via tee (needs sudo)
        proc = subprocess.run(
            ['sudo', 'tee', override_file],
            input=content.encode(),
            capture_output=True
        )
        if proc.returncode != 0:
            raise Exception(f"tee failed: {proc.stderr.decode()}")

        subprocess.run(['sudo', 'systemctl', 'daemon-reload'], check=True)
        subprocess.run(['sudo', 'systemctl', 'restart', 'angel-api'], check=True)

        # Update in-process env var so current process picks it up immediately
        os.environ['GROWW_USER_API_KEY'] = token
        global GROWW_USER_API_KEY
        GROWW_USER_API_KEY = token

        logger.info("Groww token updated successfully via API")
        return jsonify({'success': True, 'message': 'Groww token updated and service restarted'}), 200

    except Exception as e:
        logger.error(f"set-groww-token error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500



if __name__ == '__main__':
    # Check if credentials are configured
    creds = load_credentials()
    if not creds:
        logger.error("FATAL: Required environment variables not set!")
        logger.error("Please set: ANGEL_API_KEY, ANGEL_CLIENT_ID, ANGEL_PASSWORD, ANGEL_TOTP_SECRET")
        sys.exit(1)
    
    logger.info("Angel One Order Handler API Starting")
    logger.info("Listening on 0.0.0.0:5000 (accessible from network)")
    
    # Bind to 0.0.0.0 so it's accessible from EC2 instance outside localhost
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)

