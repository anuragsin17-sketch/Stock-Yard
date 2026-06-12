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

# Enable CORS for all routes (allow browser requests from GitHub Pages)
CORS(app, resources={
    r"/api/*": {
        "origins": ["*"],
        "methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": ["Content-Type"]
    },
    r"/health": {
        "origins": ["*"],
        "methods": ["GET", "OPTIONS"]
    }
})

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
        # Hardcoded credentials (since env vars aren't loading in systemd)
        token = "8253327701:AAGNFzBJ8QwKw8x8Hg-tlvWHg18DD4lgogQ"
        chat_id = "8901309420"
        
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
            shortfall_msg = f" (Need ?{validation['shortfall']:,.0f} more)" if validation['shortfall'] > 0 else ""
            telegram_msg = f"? *ORDER REJECTED*\n\n?? *Symbol:* {symbol}\n?? *Quantity:* {quantity}\n?? *Entry Price:* ?{entry_price}\n?? *Order Value:* ?{order_value:,.0f}\n\n?? *Reason:* {validation['reason']}{shortfall_msg}"
            
            if validation['balance_info']:
                telegram_msg += f"\n\n?? *Account Status:*\n� Margin Available: ?{validation['balance_info']['margin_available']:,.0f}\n� Margin Required: ?{order_value:,.0f}"
            
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
        message = f"? *ORDER PLACED*\n\n?? *Symbol:* {symbol}\n?? *Quantity:* {quantity}\n?? *Entry Price:* ?{entry_price}\n?? *Target:* ?{target_price}\n?? *Stop Loss:* ?{stop_loss}\n?? *Order Value:* ?{trade_value:,.0f}\n?? *Order ID:* `{order_id}`"
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

@app.route('/api/sync-trades', methods=['GET'])
def sync_trades():
    """Fetch all open orders from Angel One and sync with our tracking"""
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
        holdings_response = smart.getHoldings()
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
                logger.info(f"Closed trade: {ticker} - P&L: ₹{trade.get('pnl', 0):,.2f}")
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

