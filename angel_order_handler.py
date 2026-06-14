#!/usr/bin/env python3
"""
Angel One Order Handler API
Receives order confirmation from dashboard and places on Angel One
Designed to run as a systemd service on EC2 with environment variable credentials
"""

import os
import sys
import json
import secrets
import pyotp
import logging
import requests
from SmartApi import SmartConnect
from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime, timedelta

# Configure logging for systemd
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# API key for authenticating dashboard requests — set as env var on EC2
STOCKYARD_API_KEY = os.environ.get('STOCKYARD_API_KEY', '')

# Enable CORS for all routes (allow browser requests from GitHub Pages)
CORS(app, resources={
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

def get_angel_session():
    """Get authenticated SmartConnect session"""
    credentials = load_credentials()
    
    if not credentials:
        logger.error("Cannot create session: credentials not configured")
        return None
    
    try:
        logger.info("Creating SmartConnect session...")
        smart = SmartConnect(api_key=credentials.get('ANGEL_API_KEY'))
        logger.info("SmartConnect object created, generating TOTP...")
        
        totp = pyotp.TOTP(credentials.get('ANGEL_TOTP_SECRET')).now()
        logger.info(f"TOTP generated: {totp}")
        
        logger.info("Calling generateSession...")
        session = smart.generateSession(
            credentials.get('ANGEL_CLIENT_ID'),
            credentials.get('ANGEL_PASSWORD'),
            totp
        )
        logger.info(f"Session response received: {type(session)}")
        
        if not isinstance(session, dict) or not session.get('status'):
            logger.error(f"Session generation failed: {session}")
            return None
        
        logger.info("Angel One session created successfully")
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
        
        # Find exact match
        scrip_data = None
        for result in search_result['data']:
            if result.get('tradingsymbol') == symbol + '-EQ':
                scrip_data = result
                break
        
        if not scrip_data:
            scrip_data = search_result['data'][0]
        
        trading_symbol = scrip_data.get('tradingsymbol')
        symbol_token = scrip_data.get('symboltoken')
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
            "price": str(int(entry_price)),
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
            logger.error(f"Place order API call failed: {e}", exc_info=True)
            return jsonify({'success': False, 'error': f'Place order failed: {e}'}), 500
        
        if isinstance(result, str):
            order_id = result
        elif isinstance(result, dict) and result.get('status'):
            order_id = result.get('data', result.get('orderid'))
        else:
            logger.error(f"Order placement failed: Unexpected result {result}")
            return jsonify({'success': False, 'error': 'Order placement failed'}), 400
        
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
                yf_symbol = symbol.replace('-EQ', '') + '.NS'
                ticker_obj = yf.Ticker(yf_symbol)
                hist = ticker_obj.history(period='1d', interval='1m')
                if not hist.empty:
                    ltp = float(hist['Close'].iloc[-1])
                    logger.info(f"yfinance fallback (no session) for {symbol}: LTP={ltp}")
                    return jsonify({
                        'success': True,
                        'symbol': symbol,
                        'ltp': ltp,
                        'open': float(hist['Open'].iloc[0]),
                        'high': float(hist['High'].max()),
                        'low': float(hist['Low'].min()),
                        'close': ltp,
                        'volume': 0,
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
        
        # Fetch quote from Angel One
        try:
            quote_data = smart.getQuote("NSE", quote_symbol)
            logger.info(f"Quote response for {symbol}: {quote_data}")
            
            if isinstance(quote_data, dict) and quote_data.get('status'):
                data = quote_data.get('data', {})
                if isinstance(data, list) and len(data) > 0:
                    data = data[0]
                
                ltp = float(data.get('ltp', 0))
                open_price = float(data.get('open', 0))
                high = float(data.get('high', 0))
                low = float(data.get('low', 0))
                close = float(data.get('close', 0))
                volume = int(data.get('volume', 0))
                
                logger.info(f"? Quote fetched for {symbol}: LTP={ltp}, Volume={volume}")
                
                return jsonify({
                    'success': True,
                    'symbol': symbol,
                    'ltp': ltp,
                    'open': open_price,
                    'high': high,
                    'low': low,
                    'close': close,
                    'volume': volume,
                    'timestamp': datetime.now().isoformat()
                }), 200
            else:
                logger.warning(f"Quote fetch failed for {symbol}: Invalid response {quote_data}")
                return jsonify({'success': False, 'error': f'Invalid quote response: {quote_data}'}), 500
        
        except Exception as e:
            logger.error(f"getQuote API failed for {symbol}: {e}", exc_info=True)
            # Fallback to yfinance if Angel One fails
            try:
                import yfinance as yf
                yf_symbol = symbol.replace('-EQ', '') + '.NS'
                ticker_obj = yf.Ticker(yf_symbol)
                hist = ticker_obj.history(period='1d', interval='1m')
                if not hist.empty:
                    ltp = float(hist['Close'].iloc[-1])
                    logger.info(f"yfinance fallback for {symbol}: LTP={ltp}")
                    return jsonify({
                        'success': True,
                        'symbol': symbol,
                        'ltp': ltp,
                        'open': float(hist['Open'].iloc[0]) if not hist.empty else 0,
                        'high': float(hist['High'].max()) if not hist.empty else 0,
                        'low': float(hist['Low'].min()) if not hist.empty else 0,
                        'close': ltp,
                        'volume': 0,
                        'timestamp': datetime.now().isoformat(),
                        'source': 'yfinance'
                    }), 200
            except Exception as yf_e:
                logger.warning(f"yfinance fallback also failed for {symbol}: {yf_e}")
            return jsonify({'success': False, 'error': f'Quote fetch failed: {e}'}), 500
    
    except Exception as e:
        logger.error(f"Quote endpoint error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/get-52w', methods=['GET'])
def get_52w():
    """Get 52-week high/low for a stock symbol using yfinance"""
    try:
        symbol = request.args.get('symbol', '').strip()

        if not symbol:
            logger.warning("52W request: No symbol provided")
            return jsonify({'success': False, 'error': 'No symbol provided'}), 400

        logger.info(f"Fetching 52W high/low for: {symbol}")

        import yfinance as yf
        yf_symbol = symbol.upper().replace('-EQ', '') + '.NS'
        ticker_obj = yf.Ticker(yf_symbol)
        hist = ticker_obj.history(period='1y', interval='1d')

        if hist.empty:
            logger.warning(f"52W fetch: No data returned for {yf_symbol}")
            return jsonify({'success': False, 'error': f'No data found for symbol: {symbol}'}), 404

        week_52_high = round(float(hist['High'].max()), 2)
        week_52_low  = round(float(hist['Low'].min()), 2)

        logger.info(f"52W data for {symbol}: High={week_52_high}, Low={week_52_low}")
        return jsonify({
            'success': True,
            'symbol': symbol.upper().replace('-EQ', ''),
            'week_52_high': week_52_high,
            'week_52_low': week_52_low
        }), 200

    except Exception as e:
        logger.error(f"52W endpoint error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/sync-trades', methods=['GET'])
def sync_trades():
    """Fetch all open orders from Angel One and sync with our tracking"""
    # Read-only endpoint — no API key required
    try:
        logger.info("Starting trade sync with Angel One...")
        
        smart = get_angel_session()
        if not smart:
            logger.error("Trade sync failed: Could not connect to Angel One")
            return jsonify({'success': False, 'error': 'Failed to connect to Angel One'}), 401
        
        # Get all orders from Angel One
        orders_response = smart.orderBook()
        logger.info(f"Order book response: {orders_response}")
        
        if not isinstance(orders_response, dict) or not orders_response.get('status'):
            logger.warning(f"Order book fetch failed: {orders_response}")
            return jsonify({'success': False, 'error': 'Failed to fetch order book'}), 500
        
        angel_orders = orders_response.get('data', [])
        if not isinstance(angel_orders, list):
            angel_orders = []
        
        logger.info(f"Found {len(angel_orders)} orders in Angel One")
        
        # Get all positions (filled orders) from Angel One
        holdings_response = smart.holding()
        logger.info(f"Holdings response: {holdings_response}")
        
        angel_positions = []
        if isinstance(holdings_response, dict) and holdings_response.get('status'):
            angel_positions = holdings_response.get('data', [])
            if not isinstance(angel_positions, list):
                angel_positions = []
        
        logger.info(f"Found {len(angel_positions)} positions in Angel One")
        
        # Load our tracking files
        radar_trades = []
        closed_trades = []
        
        try:
            with open('radar_trades.json') as f:
                radar_trades = json.load(f)
                if not isinstance(radar_trades, list):
                    radar_trades = []
        except:
            radar_trades = []
        
        try:
            with open('closed_trades.json') as f:
                closed_trades = json.load(f)
                if not isinstance(closed_trades, list):
                    closed_trades = []
        except:
            closed_trades = []
        
        logger.info(f"Current radar trades: {len(radar_trades)}")
        logger.info(f"Current closed trades: {len(closed_trades)}")
        
        # Track changes
        updated_radar = []
        newly_closed = []
        
        # Check each radar trade for exit
        for trade in radar_trades:
            ticker = trade.get('ticker', '')
            order_id = trade.get('order_id', '')
            quantity = trade.get('quantity', 1)
            
            # Find if this order exists in Angel One and has been exited
            order_found = False
            position_found = False
            exit_price = None
            
            # Check if order still exists
            for angel_order in angel_orders:
                if str(angel_order.get('orderid')) == str(order_id):
                    order_found = True
                    logger.info(f"Order {order_id} ({ticker}) still open in Angel One")
                    break
            
            # Check if position still held - NORMALIZE SYMBOL FORMAT
            # Angel One returns "INFY-EQ" but radar_trades has "INFY"
            normalized_ticker = ticker.rstrip('-EQ')  # Remove -EQ suffix for comparison
            for position in angel_positions:
                position_symbol = position.get('tradingsymbol', '').rstrip('-EQ')
                quantity_held = int(position.get('quantity', 0))
                
                if normalized_ticker.upper() == position_symbol.upper():
                    position_found = True
                    quantity = quantity_held  # Update quantity from Angel One
                    trade['quantity'] = quantity  # Save updated quantity back to trade record
                    logger.info(f"Position {ticker} ({quantity} shares) still held in Angel One")
                    break
            
            # If order not found and position not held -> Trade exited
            if not order_found and not position_found:
                logger.warning(f"Trade {ticker} (Order {order_id}) has been exited!")
                
                # Move to closed trades
                trade['status'] = 'Closed'
                trade['closed_at'] = datetime.now().isoformat()
                
                # Calculate P&L if possible (would need exit price from Angel One)
                if 'current_price' in trade:
                    entry = float(trade.get('entry_price', 0))
                    exit_price = float(trade.get('current_price', 0))
                    qty = int(trade.get('quantity', 1))
                    
                    pnl = (exit_price - entry) * qty
                    pnl_percent = ((exit_price - entry) / entry * 100) if entry > 0 else 0
                    
                    trade['exit_price'] = exit_price
                    trade['pnl'] = pnl
                    trade['pnl_percent'] = pnl_percent
                
                newly_closed.append(trade)
                logger.info(f"Closed trade: {ticker} - P&L: ?{trade.get('pnl', 0):,.2f}")
            else:
                # Trade still open
                updated_radar.append(trade)
        
        # Add newly closed trades to closed_trades file
        if newly_closed:
            closed_trades.extend(newly_closed)
            try:
                with open('closed_trades.json', 'w') as f:
                    json.dump(closed_trades, f, indent=2)
                logger.info(f"Saved {len(closed_trades)} closed trades to closed_trades.json")
            except Exception as e:
                logger.error(f"Error saving closed trades: {e}")
        
        # Update radar trades file (removing closed ones)
        try:
            with open('radar_trades.json', 'w') as f:
                json.dump(updated_radar, f, indent=2)
            logger.info(f"Updated radar_trades.json with {len(updated_radar)} open trades")
        except Exception as e:
            logger.error(f"Error saving radar trades: {e}")
        
        return jsonify({
            'success': True,
            'radar_trades': len(updated_radar),
            'closed_trades': len(newly_closed),
            'total_positions': len(angel_positions),
            'message': f'Synced: {len(updated_radar)} open, {len(newly_closed)} newly closed'
        }), 200
        
    except Exception as e:
        logger.error(f"Trade sync error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


# ─── GROWW PORTFOLIO SYNC ──────────────────────────────────────────────────────
# Read-only: fetches open holdings from Groww and returns them in the same
# format as /api/sync-trades so the Radar tab can merge both brokers.
#
# Auth: GROWW_USER_API_KEY is the daily access token issued by Groww.
# It is used directly as a Bearer token — no exchange needed.
# Update GROWW_USER_API_KEY in the systemd override each day.

GROWW_USER_API_KEY = os.environ.get('GROWW_USER_API_KEY', '')

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

