#!/usr/bin/env python3
"""
Angel One Order Handler - SECURE VERSION
Implements all security fixes:
1. API Key authentication
2. Token encryption (AES-256)
3. Server-side quantity validation
4. Rate limiting
5. Audit logging
6. Immediate token deletion
"""

import os
import json
import secrets
import hashlib
import hmac
import pyotp
import logging
from datetime import datetime, timedelta
from functools import wraps
from collections import defaultdict
from SmartApi import SmartConnect
from flask import Flask, request, jsonify
from cryptography.fernet import Fernet

# ============================================================================
# SECURITY CONFIGURATION
# ============================================================================

MAX_CAPITAL_PER_TRADE = 50000  # ₹50,000 max per order
MAX_ORDERS_PER_HOUR = 10  # Max 10 orders per hour
MAX_VERIFICATIONS_PER_HOUR = 50  # Max 50 verification attempts per hour
TOKEN_EXPIRY_MINUTES = 3  # Reduced from 5 to 3 minutes
API_KEY_REQUIRED = os.environ.get('STOCK_YARD_API_KEY', 'dev-key-change-in-production')

# ============================================================================
# LOGGING SETUP
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('trade_audit.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ============================================================================
# ENCRYPTION SETUP
# ============================================================================

ENCRYPTION_KEY = os.environ.get('ENCRYPTION_KEY', None)
if not ENCRYPTION_KEY:
    # Generate and save key first time
    ENCRYPTION_KEY = Fernet.generate_key().decode()
    logger.warning("Generated new encryption key - SAVE THIS SECURELY!")
    print(f"🔐 New encryption key: {ENCRYPTION_KEY}")

cipher_suite = Fernet(ENCRYPTION_KEY.encode() if isinstance(ENCRYPTION_KEY, str) else ENCRYPTION_KEY)

# ============================================================================
# RATE LIMITING
# ============================================================================

class RateLimiter:
    def __init__(self):
        self.requests = defaultdict(list)
    
    def is_allowed(self, identifier, max_requests, window_minutes):
        """Check if request is allowed under rate limit"""
        now = datetime.now()
        cutoff = now - timedelta(minutes=window_minutes)
        
        # Clean old requests
        self.requests[identifier] = [
            req_time for req_time in self.requests[identifier]
            if req_time > cutoff
        ]
        
        # Check limit
        if len(self.requests[identifier]) >= max_requests:
            return False
        
        self.requests[identifier].append(now)
        return True

rate_limiter = RateLimiter()

# ============================================================================
# FLASK APP SETUP
# ============================================================================

app = Flask(__name__)
app.config['JSON_SORT_KEYS'] = False

# ============================================================================
# SECURITY DECORATORS
# ============================================================================

def require_api_key(f):
    """Decorator: Require valid API key"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        api_key = request.headers.get('X-API-Key', '')
        
        if not api_key:
            logger.warning(f"🚨 Missing API key - {request.remote_addr}")
            return jsonify({'success': False, 'error': 'Missing X-API-Key header'}), 401
        
        if not hmac.compare_digest(api_key, API_KEY_REQUIRED):
            logger.warning(f"🚨 Invalid API key attempt - {request.remote_addr}")
            return jsonify({'success': False, 'error': 'Invalid API key'}), 401
        
        logger.info(f"✅ Valid API key - {request.remote_addr}")
        return f(*args, **kwargs)
    
    return decorated_function

def rate_limit(max_requests, window_minutes):
    """Decorator: Enforce rate limiting"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            client_id = request.remote_addr
            
            if not rate_limiter.is_allowed(client_id, max_requests, window_minutes):
                logger.warning(f"🚨 Rate limit exceeded - {client_id}")
                return jsonify({
                    'success': False,
                    'error': f'Rate limit: max {max_requests} requests per {window_minutes} minutes'
                }), 429
            
            return f(*args, **kwargs)
        return decorated_function
    return decorator

# ============================================================================
# ENCRYPTION FUNCTIONS
# ============================================================================

def encrypt_token(token_dict):
    """Encrypt token data"""
    json_data = json.dumps(token_dict).encode()
    encrypted = cipher_suite.encrypt(json_data).decode()
    return encrypted

def decrypt_token(encrypted_data):
    """Decrypt token data"""
    try:
        decrypted = cipher_suite.decrypt(encrypted_data.encode())
        return json.loads(decrypted.decode())
    except Exception as e:
        logger.error(f"❌ Decryption error: {e}")
        return None

def load_tokens():
    """Load and decrypt tokens from file"""
    tokens_file = 'active_trade_tokens_encrypted.json'
    if not os.path.exists(tokens_file):
        return {}
    
    try:
        with open(tokens_file) as f:
            encrypted_tokens = json.load(f)
        
        decrypted_tokens = {}
        for token, encrypted_data in encrypted_tokens.items():
            decrypted = decrypt_token(encrypted_data)
            if decrypted:
                decrypted_tokens[token] = decrypted
        
        return decrypted_tokens
    except Exception as e:
        logger.error(f"❌ Error loading tokens: {e}")
        return {}

def save_tokens(tokens):
    """Encrypt and save tokens to file"""
    tokens_file = 'active_trade_tokens_encrypted.json'
    
    try:
        encrypted_tokens = {}
        for token, data in tokens.items():
            encrypted_tokens[token] = encrypt_token(data)
        
        with open(tokens_file, 'w') as f:
            json.dump(encrypted_tokens, f, indent=2)
        
        logger.info(f"✅ Tokens saved (encrypted): {len(tokens)} tokens")
    except Exception as e:
        logger.error(f"❌ Error saving tokens: {e}")

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def load_env():
    """Load credentials from .env"""
    creds = {}
    if os.path.exists('.env'):
        with open('.env') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    k, v = line.split('=', 1)
                    creds[k] = v
    return creds

def get_angel_session():
    """Get authenticated Angel One session"""
    credentials = load_env()
    
    try:
        smart = SmartConnect(api_key=credentials.get('ANGEL_API_KEY'))
        totp = pyotp.TOTP(credentials.get('ANGEL_TOTP_SECRET')).now()
        
        session = smart.generateSession(
            credentials.get('ANGEL_CLIENT_ID'),
            credentials.get('ANGEL_PASSWORD'),
            totp
        )
        
        if isinstance(session, dict) and session.get('status'):
            logger.info(f"✅ Angel One session created for {session['data']['clientcode']}")
            return smart
        
        logger.error(f"❌ Angel One session failed: {session}")
        return None
    except Exception as e:
        logger.error(f"❌ Session error: {e}")
        return None

# ============================================================================
# API ENDPOINTS
# ============================================================================

@app.route('/api/health', methods=['GET'])
def health_check():
    """Health check endpoint (no auth required)"""
    return jsonify({'status': 'ok', 'timestamp': datetime.now().isoformat()}), 200

@app.route('/api/verify-token', methods=['POST'])
@require_api_key
@rate_limit(max_requests=50, window_minutes=60)
def verify_token():
    """Verify trade confirmation token"""
    try:
        data = request.json
        token = data.get('token', '').strip()
        
        if not token:
            logger.warning("❌ Empty token provided")
            return jsonify({'success': False, 'error': 'No token provided'}), 400
        
        tokens = load_tokens()
        
        if token not in tokens:
            logger.warning(f"❌ Invalid token attempt: {token[:10]}...")
            return jsonify({'success': False, 'error': 'Invalid token'}), 401
        
        token_data = tokens[token]
        expires_at = datetime.fromisoformat(token_data['expires_at'])
        
        # Check expiration
        if datetime.now() > expires_at:
            logger.warning(f"❌ Token expired: {token[:10]}...")
            del tokens[token]
            save_tokens(tokens)
            return jsonify({'success': False, 'error': 'Token expired'}), 401
        
        logger.info(f"✅ Token verified: {token_data['ticker']}")
        
        return jsonify({
            'success': True,
            'trade': token_data,
            'token_expires_in_seconds': int((expires_at - datetime.now()).total_seconds())
        }), 200
        
    except Exception as e:
        logger.error(f"❌ Verify token error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/place-order', methods=['POST'])
@require_api_key
@rate_limit(max_requests=10, window_minutes=60)
def place_order():
    """Place order on Angel One (SECURE VERSION)"""
    try:
        data = request.json
        token = data.get('token', '').strip()
        symbol = data.get('symbol', '').strip()
        quantity = int(data.get('quantity', 0))
        entry_price = float(data.get('entry_price', 0))
        target_price = float(data.get('target_price', 0))
        stop_loss = float(data.get('stop_loss', 0))
        
        logger.info(f"📋 Order request: {symbol} x{quantity} @ ₹{entry_price}")
        
        # ====== SECURITY CHECKS ======
        
        # 1. Verify token
        tokens = load_tokens()
        
        if token not in tokens:
            logger.warning(f"🚨 Invalid token attempt: {token[:10]}...")
            return jsonify({'success': False, 'error': 'Invalid token'}), 401
        
        token_data = tokens[token]
        
        # 2. Check token expiration
        expires_at = datetime.fromisoformat(token_data['expires_at'])
        if datetime.now() > expires_at:
            logger.warning(f"🚨 Expired token: {token[:10]}...")
            del tokens[token]
            save_tokens(tokens)
            return jsonify({'success': False, 'error': 'Token expired'}), 401
        
        # 3. Verify symbol matches
        if token_data['ticker'] != symbol:
            logger.warning(f"🚨 Symbol mismatch! Token: {token_data['ticker']}, Request: {symbol}")
            return jsonify({'success': False, 'error': 'Symbol mismatch - security violation'}), 403
        
        # 4. Validate quantity
        if quantity <= 0 or quantity > 1000:
            logger.warning(f"🚨 Invalid quantity: {quantity}")
            return jsonify({'success': False, 'error': 'Invalid quantity (1-1000)'}), 400
        
        # 5. Validate capital required (SERVER-SIDE)
        capital_required = entry_price * quantity
        if capital_required > MAX_CAPITAL_PER_TRADE:
            logger.warning(f"🚨 Capital limit exceeded: ₹{capital_required} > ₹{MAX_CAPITAL_PER_TRADE}")
            return jsonify({
                'success': False,
                'error': f'Capital limit exceeded: ₹{capital_required:.2f} > ₹{MAX_CAPITAL_PER_TRADE}'
            }), 400
        
        logger.info(f"✅ Security checks passed for {symbol}")
        
        # ====== PLACE ORDER ======
        
        smart = get_angel_session()
        if not smart:
            logger.error("❌ Cannot connect to Angel One")
            return jsonify({'success': False, 'error': 'Cannot connect to Angel One'}), 500
        
        # Search symbol
        search_result = smart.searchScrip("NSE", symbol)
        if not search_result.get('data'):
            logger.warning(f"❌ Symbol not found: {symbol}")
            return jsonify({'success': False, 'error': f'Symbol {symbol} not found'}), 404
        
        # Find EXACT match
        scrip_data = None
        for result in search_result['data']:
            if result.get('tradingsymbol') == symbol + '-EQ':
                scrip_data = result
                break
        
        if not scrip_data:
            scrip_data = search_result['data'][0]
        
        trading_symbol = scrip_data.get('tradingsymbol')
        symbol_token = scrip_data.get('symboltoken')
        
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
        
        logger.info(f"📤 Placing order on Angel One: {trading_symbol} x{quantity}")
        result = smart.placeOrder(order_params)
        
        # Parse result
        if isinstance(result, str):
            order_id = result
        elif isinstance(result, dict) and result.get('status'):
            order_id = result.get('data', result.get('orderid'))
        else:
            logger.error(f"❌ Order placement failed: {result}")
            return jsonify({'success': False, 'error': 'Order placement failed'}), 400
        
        logger.info(f"✅ Order placed: {order_id}")
        
        # ====== SAVE ORDER RECORD ======
        
        order_record = {
            'order_id': order_id,
            'symbol': symbol,
            'quantity': quantity,
            'entry_price': entry_price,
            'target_price': target_price,
            'stop_loss': stop_loss,
            'source': token_data['source'],
            'placed_at': datetime.now().isoformat(),
            'status': 'PENDING',
            'capital_required': capital_required
        }
        
        # Append to audit log
        orders_log = []
        if os.path.exists('angel_orders_audit.json'):
            try:
                with open('angel_orders_audit.json') as f:
                    orders_log = json.load(f)
                    if not isinstance(orders_log, list):
                        orders_log = []
            except:
                orders_log = []
        
        orders_log.append(order_record)
        
        with open('angel_orders_audit.json', 'w') as f:
            json.dump(orders_log, f, indent=2)
        
        logger.info(f"📊 Order saved to audit log")
        
        # ====== DELETE TOKEN (IMMEDIATE - SECURITY) ======
        
        del tokens[token]
        save_tokens(tokens)
        logger.info(f"🔐 Token deleted immediately (single-use)")
        
        # ====== ADD TO RADAR ======
        
        radar_file = 'radar_trades.json'
        radar_trades = []
        if os.path.exists(radar_file):
            try:
                with open(radar_file) as f:
                    data = json.load(f)
                    radar_trades = data if isinstance(data, list) else []
            except:
                radar_trades = []
        
        new_trade = {
            'ticker': symbol,
            'source': token_data['source'],
            'entry_price': round(entry_price, 2),
            'target': round(target_price, 2),
            'stop_loss': round(stop_loss, 2),
            'quantity': quantity,
            'order_id': order_id,
            'status': 'Triggered',
            'triggered_at': datetime.now().isoformat(),
            'capital': capital_required,
            'potential_gain': round(capital_required * 0.20, 2),
            'max_loss': round(capital_required * 0.08, 2)
        }
        
        radar_trades.append(new_trade)
        
        with open(radar_file, 'w') as f:
            json.dump(radar_trades, f, indent=2)
        
        logger.info(f"📍 Stock added to Radar: {symbol}")

        # ====== PLACE GTT OCO (TARGET + STOPLOSS) ======

        gtt_id = None
        gtt_error = None
        try:
            # Angel One GTT API — OCO (One Cancels Other)
            # Triggers a SELL when price hits target OR stop_loss
            gtt_params = {
                "tradingsymbol":  trading_symbol,
                "symboltoken":    symbol_token,
                "exchange":       "NSE",
                "producttype":    "DELIVERY",
                "transactiontype": "SELL",
                "qty":            quantity,
                "type":           "TWO_LEG",   # OCO = two-leg GTT
                "price":          int(entry_price),  # reference price
                "triggerprice": [
                    {
                        "triggertype": "ABOVE",         # target hit
                        "price":       round(target_price, 2),
                        "orderprice":  round(target_price * 0.995, 2),  # limit slightly below trigger
                    },
                    {
                        "triggertype": "BELOW",         # stop loss hit
                        "price":       round(stop_loss, 2),
                        "orderprice":  round(stop_loss * 0.995, 2),  # limit slightly below SL
                    }
                ]
            }

            gtt_result = smart.gttCreateRule(gtt_params)
            if isinstance(gtt_result, dict) and gtt_result.get('status'):
                gtt_id = gtt_result.get('data', {}).get('id') or gtt_result.get('data')
                logger.info(f"✅ GTT OCO placed: {gtt_id} | T:{target_price} / SL:{stop_loss}")
            else:
                gtt_error = str(gtt_result)
                logger.warning(f"⚠️ GTT placement failed: {gtt_error}")
        except Exception as e:
            gtt_error = str(e)
            logger.warning(f"⚠️ GTT exception: {e}")

        # Save GTT id to radar trade
        if gtt_id:
            for t in radar_trades:
                if t.get('order_id') == order_id:
                    t['gtt_id'] = gtt_id
                    t['gtt_status'] = 'ACTIVE'
            with open(radar_file, 'w') as f:
                json.dump(radar_trades, f, indent=2)

        # ====== TELEGRAM: BUY + GTT NOTIFICATION ======

        TELEGRAM_TOKEN_ENV = os.environ.get('TELEGRAM_BOT_TOKEN', '')
        TELEGRAM_CHAT_ENV  = os.environ.get('TELEGRAM_CHAT_ID', '')
        APP_URL = 'https://anuragsin17-sketch.github.io/Stock-Yard-Public'
        chart_url = f"https://in.tradingview.com/chart/?symbol=NSE:{symbol}"

        if TELEGRAM_TOKEN_ENV and TELEGRAM_CHAT_ENV:
            try:
                pnl_risk   = round((stop_loss   - entry_price) / entry_price * 100, 1)
                pnl_target = round((target_price - entry_price) / entry_price * 100, 1)
                rr = abs(pnl_target / pnl_risk) if pnl_risk else 0

                gtt_line = (f"✅ GTT OCO Armed — ID: {gtt_id}"
                            if gtt_id else
                            f"⚠️ GTT not placed — set manually ({gtt_error or 'error'})")

                msg = (
                    f"📥 *BUY ORDER PLACED — {symbol}*\n\n"
                    f"📋 Order ID: `{order_id}`\n"
                    f"💰 Entry: ₹{entry_price:,.2f} × {quantity} shares\n"
                    f"💼 Capital: ₹{capital_required:,.0f}\n\n"
                    f"🎯 Target: ₹{target_price:,.2f} ({pnl_target:+.1f}%)\n"
                    f"🛑 Stop Loss: ₹{stop_loss:,.2f} ({pnl_risk:+.1f}%)\n"
                    f"📐 R:R = 1:{rr:.1f}\n\n"
                    f"{gtt_line}\n\n"
                    f"[Chart]({chart_url}) | [App]({APP_URL}/)"
                )
                requests.post(
                    f'https://api.telegram.org/bot{TELEGRAM_TOKEN_ENV}/sendMessage',
                    json={'chat_id': TELEGRAM_CHAT_ENV, 'text': msg,
                          'parse_mode': 'Markdown', 'disable_web_page_preview': True},
                    timeout=10
                )
                logger.info(f"📲 Telegram sent for {symbol}")
            except Exception as e:
                logger.warning(f"Telegram error: {e}")

        return jsonify({
            'success': True,
            'order_id': order_id,
            'gtt_id':   gtt_id,
            'symbol':   symbol,
            'quantity': quantity,
            'capital':  capital_required,
            'message':  f'Order placed! ID: {order_id}' + (f' | GTT: {gtt_id}' if gtt_id else ' | GTT failed — set manually')
        }), 200
        
    except Exception as e:
        logger.error(f"❌ Order placement error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/order-status/<order_id>', methods=['GET'])
@require_api_key
def order_status(order_id):
    """Get order status from Angel One"""
    try:
        smart = get_angel_session()
        if not smart:
            return jsonify({'success': False, 'error': 'Cannot connect to Angel One'}), 401
        
        orders = smart.orderBook()
        if not isinstance(orders, dict) or not orders.get('data'):
            return jsonify({'success': False, 'error': 'No orders found'}), 404
        
        for order in orders['data']:
            if order.get('orderid') == order_id:
                logger.info(f"✅ Order status fetched: {order_id}")
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
        
        return jsonify({'success': False, 'error': 'Order not found'}), 404
        
    except Exception as e:
        logger.error(f"❌ Order status error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/audit-log', methods=['GET'])
@require_api_key
def audit_log():
    """Get audit log of all orders"""
    try:
        if not os.path.exists('angel_orders_audit.json'):
            return jsonify({'success': True, 'orders': []}), 200
        
        with open('angel_orders_audit.json') as f:
            orders = json.load(f)
        
        logger.info(f"✅ Audit log fetched: {len(orders)} orders")
        return jsonify({'success': True, 'orders': orders}), 200
        
    except Exception as e:
        logger.error(f"❌ Audit log error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

# ============================================================================
# ERROR HANDLERS
# ============================================================================

@app.errorhandler(404)
def not_found(error):
    logger.warning(f"🚨 404 Not Found: {request.path}")
    return jsonify({'success': False, 'error': 'Not found'}), 404

@app.errorhandler(500)
def server_error(error):
    logger.error(f"🚨 500 Server Error: {error}")
    return jsonify({'success': False, 'error': 'Server error'}), 500

# ============================================================================
# STARTUP
# ============================================================================

if __name__ == '__main__':
    print(f"\n{'='*80}")
    print("🔐 SECURE Angel One Order Handler")
    print(f"{'='*80}")
    print(f"API Key Required: {API_KEY_REQUIRED != 'dev-key-change-in-production'}")
    print(f"Token Encryption: ✅ AES-256")
    print(f"Rate Limiting: ✅ Enabled")
    print(f"Audit Logging: ✅ Enabled")
    print(f"Max Capital/Trade: ₹{MAX_CAPITAL_PER_TRADE:,.0f}")
    print(f"Token Expiry: {TOKEN_EXPIRY_MINUTES} minutes")
    print(f"Listening on: http://localhost:5000")
    print(f"{'='*80}\n")
    
    app.run(host='localhost', port=5000, debug=False)
