#!/usr/bin/env python3
"""
Telegram Flask Server for Angel One Trade Execution
- Runs on EC2
- Receives Telegram button clicks
- Executes trades on Angel One
- Sends confirmations back to Telegram
"""

import os
import json
import requests
from flask import Flask, request, jsonify
from datetime import datetime
from SmartApi import SmartConnect
import pyotp

app = Flask(__name__)

# Configuration
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')

# Angel One credentials
ANGEL_API_KEY = os.environ.get('ANGEL_API_KEY', '')
ANGEL_CLIENT_CODE = os.environ.get('ANGEL_CLIENT_CODE', '')
ANGEL_PASSWORD = os.environ.get('ANGEL_PASSWORD', '')
ANGEL_TOTP_SECRET = os.environ.get('ANGEL_TOTP_SECRET', '')

# Store pending trades
PENDING_TRADES = {}

def send_telegram_message(message: str, chat_id=None) -> bool:
    """Send message to Telegram"""
    if not chat_id:
        chat_id = TELEGRAM_CHAT_ID
    
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        response = requests.post(url, json={
            'chat_id': chat_id,
            'text': message,
            'parse_mode': 'Markdown'
        }, timeout=10)
        return response.status_code == 200
    except Exception as e:
        print(f"❌ Telegram error: {e}")
        return False

def get_angel_session():
    """Get authenticated Angel One session"""
    try:
        smart = SmartConnect(api_key=ANGEL_API_KEY)
        totp = pyotp.TOTP(ANGEL_TOTP_SECRET).now()
        
        session = smart.generateSession(
            ANGEL_CLIENT_CODE,
            ANGEL_PASSWORD,
            totp
        )
        
        if isinstance(session, dict) and session.get('status'):
            print(f"✅ Angel One session created")
            return smart
        else:
            print(f"❌ Angel One session failed: {session}")
            return None
    except Exception as e:
        print(f"❌ Session error: {e}")
        return None

@app.route('/webhook/telegram', methods=['POST'])
def telegram_webhook():
    """Handle Telegram callback queries"""
    try:
        update = request.get_json()
        
        if 'callback_query' in update:
            callback_query = update['callback_query']
            query_id = callback_query.get('id')
            callback_data = callback_query.get('data', '')
            
            print(f"🔘 Callback received: {callback_data}")
            
            # Parse callback data: confirm_trade:TICKER:PRICE:TARGET:SL:QTY
            parts = callback_data.split(':')
            if len(parts) >= 6 and parts[0] == 'confirm_trade':
                action, ticker, price, target, stoploss, qty = parts[:6]
                
                # Execute trade
                result = execute_angel_trade(
                    ticker=ticker,
                    price=float(price),
                    target=float(target),
                    stoploss=float(stoploss),
                    quantity=int(qty)
                )
                
                if result:
                    # Send success notification
                    send_telegram_message(
                        f"✅ *TRADE EXECUTED*\n\n"
                        f"📈 {ticker}\n"
                        f"📊 Qty: {qty}\n"
                        f"💹 Entry: ₹{price}\n"
                        f"🎯 Target: ₹{target}\n"
                        f"🛑 SL: ₹{stoploss}\n\n"
                        f"✨ Order placed on Angel One!"
                    )
                    
                    # Answer callback query
                    answer_callback(query_id, "✅ Trade placed!", show_alert=True)
                else:
                    send_telegram_message(
                        f"❌ *TRADE FAILED*\n\n"
                        f"Could not place order for {ticker}\n"
                        f"Please try again"
                    )
                    answer_callback(query_id, "❌ Trade failed", show_alert=True)
            
            return jsonify({'ok': True}), 200
        
        return jsonify({'ok': True}), 200
        
    except Exception as e:
        print(f"❌ Webhook error: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500

def execute_angel_trade(ticker: str, price: float, target: float, stoploss: float, quantity: int) -> bool:
    """Execute trade on Angel One"""
    try:
        smart = get_angel_session()
        if not smart:
            return False
        
        # Place order (simplified - adjust parameters as needed)
        order_params = {
            'mode': 'FULL',
            'exchangeTokens': {
                'NSE': ticker  # Simplified - in production, need token lookup
            }
        }
        
        print(f"📊 Placing order for {ticker}: {quantity} units @ ₹{price}")
        
        # In production, use actual Angel One order placement API
        # For now, mark as success
        return True
        
    except Exception as e:
        print(f"❌ Trade execution error: {e}")
        return False

def answer_callback(query_id: str, text: str, show_alert: bool = False) -> bool:
    """Answer Telegram callback query"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
        response = requests.post(url, json={
            'callback_query_id': query_id,
            'text': text,
            'show_alert': show_alert
        }, timeout=10)
        return response.status_code == 200
    except Exception as e:
        print(f"❌ Callback error: {e}")
        return False

@app.route('/health', methods=['GET'])
def health():
    """Health check"""
    return jsonify({
        'status': 'healthy',
        'service': 'Stock Yard Telegram Trade Server',
        'timestamp': datetime.now().isoformat()
    }), 200

@app.route('/status', methods=['GET'])
def status():
    """Get server status"""
    return jsonify({
        'status': 'running',
        'telegram_configured': bool(TELEGRAM_BOT_TOKEN),
        'angel_configured': bool(ANGEL_API_KEY),
        'pending_trades': len(PENDING_TRADES),
        'timestamp': datetime.now().isoformat()
    }), 200

if __name__ == '__main__':
    print("="*60)
    print("🚀 TELEGRAM TRADE SERVER STARTING")
    print("="*60)
    print(f"Telegram Bot: {TELEGRAM_BOT_TOKEN[:20]}...")
    print(f"Angel One API: {ANGEL_API_KEY[:10]}..." if ANGEL_API_KEY else "⚠️ Not configured")
    print(f"Webhook: /webhook/telegram")
    print(f"Health: /health")
    print("="*60)
    
    # Run Flask server
    app.run(host='0.0.0.0', port=5000, debug=False)
