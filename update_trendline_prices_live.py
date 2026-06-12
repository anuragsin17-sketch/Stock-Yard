#!/usr/bin/env python3
"""
Update trendline_screen.json with live prices from Angel One API
Run this before workflow or separately to refresh prices
"""

import json
import requests
from datetime import datetime

TRENDLINE_FILE = 'trendline_screen.json'
API_BASE = 'http://32.194.58.75:5000'

def get_live_price(ticker: str) -> float:
    """Get current price from Angel One API"""
    try:
        symbol = ticker.replace('.NS', '')  # Remove .NS suffix
        api_url = f"{API_BASE}/api/get-quote?symbol={symbol}"
        
        response = requests.get(api_url, timeout=5)
        
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

def main():
    print(f"\n{'='*60}")
    print(f"UPDATING TRENDLINE PRICES - {datetime.now().strftime('%Y-%m-%d %H:%M IST')}")
    print(f"{'='*60}\n")
    
    # Load trendline data
    try:
        with open(TRENDLINE_FILE) as f:
            stocks = json.load(f)
    except Exception as e:
        print(f"❌ Error loading {TRENDLINE_FILE}: {e}")
        return
    
    if not isinstance(stocks, list):
        print(f"❌ {TRENDLINE_FILE} is not a list")
        return
    
    print(f"📊 Updating prices for {len(stocks)} stocks from Angel One API...\n")
    
    updated_count = 0
    for stock in stocks:
        ticker = stock.get('ticker', '')
        if not ticker:
            continue
        
        old_price = stock.get('currentPrice', 0)
        new_price = get_live_price(ticker)
        
        if new_price and new_price > 0:
            stock['currentPrice'] = round(new_price, 2)
            price_change = ((new_price - old_price) / old_price * 100) if old_price > 0 else 0
            
            # Update distance remaining
            trigger_price = stock.get('triggerPrice', 0)
            if trigger_price > 0:
                distance = ((trigger_price - new_price) / trigger_price * 100)
                stock['distanceRemaining'] = round(distance, 2)
            
            print(f"✅ {ticker}: ₹{old_price:.2f} → ₹{new_price:.2f} ({price_change:+.2f}%)")
            updated_count += 1
        else:
            print(f"⊘ {ticker}: Could not fetch price (using cached ₹{old_price:.2f})")
    
    # Save updated data
    try:
        with open(TRENDLINE_FILE, 'w') as f:
            json.dump(stocks, f, indent=2)
        print(f"\n✅ Updated {updated_count}/{len(stocks)} prices")
        print(f"📁 Saved to {TRENDLINE_FILE}")
    except Exception as e:
        print(f"❌ Error saving {TRENDLINE_FILE}: {e}")
    
    print(f"\n{'='*60}\n")

if __name__ == "__main__":
    main()
