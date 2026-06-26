#!/usr/bin/env python3
"""
Macro Fibonacci Scanner
Adds stocks at key Fibonacci retracement levels (March 2020 low to ATH)
WITHOUT requiring an ascending trendline.
"""

import json
import yfinance as yf
import pandas as pd
from datetime import datetime

# Target Fibonacci levels to look for (retracement from ATH)
TARGET_FIB_LEVELS = ['50.0%', '61.8%', '78.6%', '100.0%']
PROXIMITY_THRESHOLD = 5.0  # Within 5% of Fibonacci level

def calculate_macro_fib(ticker):
    """
    Calculate macro Fibonacci levels from March 2020 low to ATH.
    For stocks without 2020 data, use their listing date low to ATH.
    """
    try:
        # Don't add .NS if already present
        if not ticker.endswith('.NS'):
            ticker = f"{ticker}.NS"
        
        stock = yf.Ticker(ticker)
        
        # Try to get data from March 2020, but if not available, get max available history
        df = stock.history(start="2020-03-01", interval="1mo")
        
        # If no data from 2020, try to get all available history
        if df is None or df.empty or len(df) < 6:
            df = stock.history(period="max", interval="1mo")
        
        if df is None or df.empty or len(df) < 6:
            return None
        
        # Get the historical low (either March 2020 low or earliest available low)
        march_2020_mask = df.index < pd.Timestamp('2020-04-01', tz=df.index.tz)
        if march_2020_mask.sum() > 0:
            # Stock has March 2020 data - use COVID crash low
            historical_low = df.loc[march_2020_mask, 'Low'].min()
            base_label = "March 2020"
        else:
            # Stock listed after March 2020 - use listing date low
            # Get the low from first 3 months of available data
            first_months = min(3, len(df))
            historical_low = df['Low'].iloc[:first_months].min()
            base_label = df.index[0].strftime('%b %Y')
        
        if pd.isna(historical_low) or historical_low <= 0:
            return None
        
        # All-Time High
        ath_price = df['High'].max()
        if pd.isna(ath_price) or ath_price <= 0:
            return None
            
        ath_date = df['High'].idxmax()
        
        # Current price
        current = df['Close'].iloc[-1]
        if pd.isna(current) or current <= 0:
            return None
        
        # Fibonacci range
        fib_range = ath_price - historical_low
        
        if fib_range <= 0:
            return None
        
        # Calculate Fibonacci retracement levels (from ATH down)
        fib_levels = {
            '23.6%': ath_price - (fib_range * 0.236),
            '38.2%': ath_price - (fib_range * 0.382),
            '50.0%': ath_price - (fib_range * 0.500),
            '61.8%': ath_price - (fib_range * 0.618),
            '78.6%': ath_price - (fib_range * 0.786),
            '100.0%': historical_low,
        }
        
        # Find closest TARGET Fibonacci level (only check 50%, 61.8%, 78.6%, 100%)
        closest_fib = None
        min_dist = float('inf')
        
        for level, price in fib_levels.items():
            # Only check TARGET_FIB_LEVELS
            if level not in TARGET_FIB_LEVELS:
                continue
            if pd.isna(price) or price <= 0:
                continue
            dist_pct = abs((current - price) / price * 100)
            if dist_pct < min_dist:
                min_dist = dist_pct
                closest_fib = (level, price)
        
        if closest_fib is None:
            return None
        
        # Only include if within threshold
        if min_dist <= PROXIMITY_THRESHOLD:
            return {
                'ticker': ticker,
                'currentPrice': round(current, 2),
                'ath': round(ath_price, 2),
                'ath_date': ath_date.strftime('%Y-%m-%d'),
                'march_2020_low': round(historical_low, 2),
                'base_label': base_label,  # "March 2020" or listing month
                'fib_level': closest_fib[0],
                'fib_price': round(closest_fib[1], 2),
                'distance_pct': round(min_dist, 2),
                'all_fib_levels': {k: round(v, 2) for k, v in fib_levels.items()}
            }
        
        return None
        
    except Exception as e:
        # Silently ignore errors for stocks with data issues
        return None


def scan_macro_fib(stock_list):
    """Scan stocks for macro Fibonacci levels."""
    print(f"\n{'='*70}")
    print(f"  MACRO FIBONACCI SCANNER")
    print(f"  Target Levels: {', '.join(TARGET_FIB_LEVELS)}")
    print(f"  Proximity: ±{PROXIMITY_THRESHOLD}%")
    print(f"{'='*70}\n")
    
    results = []
    
    for i, ticker in enumerate(stock_list, 1):
        print(f"[{i}/{len(stock_list)}] Scanning {ticker}...", end=' ')
        
        result = calculate_macro_fib(ticker)
        
        if result:
            print(f"✅ {result['fib_level']} (₹{result['fib_price']:,.2f}, {result['distance_pct']:+.2f}%)")
            results.append(result)
        else:
            print("❌")
    
    return results


if __name__ == '__main__':
    # Nifty 50 stocks
    NIFTY_50 = [
        'ADANIENT','ADANIPORTS','APOLLOHOSP','ASIANPAINT','AXISBANK',
        'BAJAJ-AUTO','BAJAJFINSV','BAJFINANCE','BHARTIARTL','BPCL',
        'BRITANNIA','CIPLA','COALINDIA','DIVISLAB','DRREDDY',
        'EICHERMOT','GRASIM','HCLTECH','HDFCBANK','HDFCLIFE',
        'HEROMOTOCO','HINDALCO','HINDUNILVR','ICICIBANK','INDUSINDBK',
        'INFY','ITC','JSWSTEEL','KOTAKBANK','LT',
        'M&M','MARUTI','NESTLEIND','NTPC','ONGC',
        'POWERGRID','RELIANCE','SBILIFE','SBIN','SHRIRAMFIN',
        'SUNPHARMA','TATACONSUM','TATAMOTORS','TATASTEEL','TCS',
        'TECHM','TITAN','ULTRACEMCO','WIPRO','LTIM'
    ]
    
    results = scan_macro_fib(NIFTY_50)
    
    print(f"\n{'='*70}")
    print(f"  RESULTS: {len(results)} stocks found")
    print(f"{'='*70}\n")
    
    for r in results:
        print(f"📊 {r['ticker']}")
        print(f"   Current: ₹{r['currentPrice']:,.2f}")
        print(f"   Fib Level: {r['fib_level']} at ₹{r['fib_price']:,.2f}")
        print(f"   Distance: {r['distance_pct']:+.2f}%")
        print(f"   ATH: ₹{r['ath']:,.2f} ({r['ath_date']})")
        print()
    
    # Save to JSON
    output_file = 'macro_fib_signals.json'
    with open(output_file, 'w') as f:
        json.dump(results, f, indent=2)
    
    print(f"✅ Saved to {output_file}")
