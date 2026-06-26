#!/usr/bin/env python3
"""
Trendline Cache Manager
- Builds trendlines ONCE and saves to JSON
- Daily scanner just reads cache + checks current price
- Rebuilds only on weekends or when forced
"""

import yfinance as yf
import pandas as pd
import numpy as np
from scipy.signal import argrelextrema
from datetime import datetime, timedelta
import json
import os

CACHE_FILE = 'trendline_cache.json'
CACHE_MAX_AGE_DAYS = 7  # Rebuild weekly

# ─── BUILD TRENDLINE ─────────────────────────────────────────────────────────

def get_sector_order(ticker):
    banking = ['SBIN', 'HDFCBANK', 'ICICIBANK', 'AXISBANK', 'KOTAKBANK',
               'INDUSINDBK', 'FEDERALBNK', 'BANDHANBNK', 'RBLBANK', 'IDFCFIRSTB']
    return 6 if any(b in ticker.upper() for b in banking) else 10

def build_trendline(ticker):
    """
    Build trendline with wick-based touch detection.
    PRIMARY:  Monthly 8 years
    FALLBACK: Weekly 5 years (if monthly gives < 3 wick touches)
    """
    def from_df(df, timeframe):
        if df.empty or len(df) < 18:
            return None
        df = df.dropna()
        df['Price_Idx'] = np.arange(len(df))
        low_prices = df['Low'].values.flatten()

        order = get_sector_order(ticker)
        if timeframe == 'weekly':
            order = max(3, order // 2)

        touchbacks = argrelextrema(low_prices, np.less, order=order)
        for fallback in [8, 6, 5, 4, 3]:
            if len(touchbacks[0]) >= 2:
                break
            touchbacks = argrelextrema(low_prices, np.less, order=fallback)

        if len(touchbacks[0]) < 2:
            return None

        num = min(3, len(touchbacks[0]))
        anchor_indices = touchbacks[0][-num:]
        x = [int(df['Price_Idx'].iloc[i]) for i in anchor_indices]
        y = [float(low_prices[i]) for i in anchor_indices]
        slope, intercept = np.polyfit(x, y, 1)

        if slope <= 0:
            return None

        # Count wick touches (within 8% of trendline)
        wick_touches = []
        for i in range(len(df)):
            midx = int(df['Price_Idx'].iloc[i])
            tl = (slope * midx) + intercept
            dist = abs((float(low_prices[i]) - tl) / tl) * 100
            if dist <= 8.0:
                wick_touches.append({
                    'date': df.index[i].strftime('%Y-%m-%d'),
                    'price': round(float(low_prices[i]), 2),
                    'dist_pct': round(dist, 2)
                })

        if len(wick_touches) < 3:
            return None

        last_touch_idx = int(anchor_indices[-1])
        last_touch_price = float(low_prices[last_touch_idx])
        swing_high = float(df.iloc[last_touch_idx:]['High'].max())
        fib_range = swing_high - last_touch_price

        fib_levels = {}
        if fib_range > 0:
            fib_levels = {
                '23.6%': round(swing_high - (fib_range * 0.236), 2),
                '38.2%': round(swing_high - (fib_range * 0.382), 2),
                '50.0%': round(swing_high - (fib_range * 0.500), 2),
                '61.8%': round(swing_high - (fib_range * 0.618), 2),
                '78.6%': round(swing_high - (fib_range * 0.786), 2)
            }

        return {
            'ticker': ticker.replace('.NS', ''),
            'slope': round(float(slope), 6),
            'intercept': round(float(intercept), 4),
            'last_month_idx': int(df['Price_Idx'].iloc[-1]),
            'last_month_date': df.index[-1].strftime('%Y-%m-%d'),
            'wick_touch_count': len(wick_touches),
            'last_3_touches': wick_touches[-3:],
            'fib_levels': fib_levels,
            'last_touch_price': round(last_touch_price, 2),
            'swing_high': round(swing_high, 2),
            'timeframe': timeframe,
            'built_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }

    try:
        # PRIMARY: Monthly 8 years
        df_monthly = yf.download(ticker, period="8y", interval="1mo",
                                  auto_adjust=True, progress=False)
        # Fix multi-level columns from yfinance
        if isinstance(df_monthly.columns, pd.MultiIndex):
            df_monthly.columns = df_monthly.columns.get_level_values(0)
        result = from_df(df_monthly, 'monthly')
        if result:
            return result

        # FALLBACK: Weekly 5 years
        df_weekly = yf.download(ticker, period="5y", interval="1wk",
                                 auto_adjust=True, progress=False)
        if isinstance(df_weekly.columns, pd.MultiIndex):
            df_weekly.columns = df_weekly.columns.get_level_values(0)
        return from_df(df_weekly, 'weekly')

    except Exception:
        return None

# ─── CACHE MANAGEMENT ────────────────────────────────────────────────────────

def load_cache():
    """Load existing trendline cache"""
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return {'built_at': None, 'trendlines': {}}

def save_cache(cache):
    """Save trendline cache to JSON"""
    with open(CACHE_FILE, 'w') as f:
        json.dump(cache, f, indent=2)

def is_cache_stale(cache):
    """Check if cache needs rebuilding (older than 7 days)"""
    if not cache.get('built_at'):
        return True
    built_at = datetime.strptime(cache['built_at'], '%Y-%m-%d %H:%M:%S')
    age_days = (datetime.now() - built_at).days
    return age_days >= CACHE_MAX_AGE_DAYS

def build_full_cache(tickers, force=False):
    """
    Build trendline cache for all stocks.
    Only rebuilds if cache is stale or force=True.
    """
    cache = load_cache()

    if not force and not is_cache_stale(cache):
        print(f"✅ Cache is fresh (built: {cache['built_at']})")
        print(f"   Trendlines cached: {len(cache['trendlines'])}")
        print(f"   Next rebuild: in {CACHE_MAX_AGE_DAYS - (datetime.now() - datetime.strptime(cache['built_at'], '%Y-%m-%d %H:%M:%S')).days} days")
        return cache

    print(f"🔄 Building trendline cache for {len(tickers)} stocks...")
    print(f"   (This runs ONCE per week, not every day)")
    print("-" * 50)

    trendlines = {}
    failed = 0

    for i, ticker in enumerate(tickers, 1):
        try:
            tl = build_trendline(ticker)
            if tl:
                trendlines[ticker] = tl
            else:
                failed += 1
        except Exception as e:
            failed += 1
            if i <= 5:  # Print first 5 errors for debugging
                print(f"   ERROR {ticker}: {e}")

        if i % 50 == 0:
            print(f"   Progress: {i}/{len(tickers)} | Valid: {len(trendlines)} | Failed: {failed}")
            # Save partial cache as we go
            cache = {
                'built_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'total_stocks': len(tickers),
                'valid_trendlines': len(trendlines),
                'trendlines': trendlines
            }
            save_cache(cache)

    # Final save
    cache = {
        'built_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'total_stocks': len(tickers),
        'valid_trendlines': len(trendlines),
        'trendlines': trendlines
    }
    save_cache(cache)

    print(f"\n✅ Cache built: {len(trendlines)}/{len(tickers)} valid trendlines")
    print(f"💾 Saved to: {CACHE_FILE}")
    return cache

# ─── DAILY SCANNER (uses cache) ──────────────────────────────────────────────

def get_trigger_for_today(tl):
    """
    Get today's trendline trigger price using imaginary vertical line.
    Uses cached slope/intercept - NO data download needed!
    """
    today = datetime.now()
    last_month_date = datetime.strptime(tl['last_month_date'], '%Y-%m-%d')

    months_diff = ((today.year - last_month_date.year) * 12 +
                   (today.month - last_month_date.month))

    current_idx = tl['last_month_idx'] + months_diff
    trigger = (tl['slope'] * current_idx) + tl['intercept']
    return round(trigger, 2)

def get_fib_confluence(tl, trigger_price):
    """Fibonacci confluence from cached data - within 2% only"""
    try:
        fib_levels = tl.get('fib_levels', {})
        if not fib_levels:
            return 6, "No Fib data"

        min_dist = float('inf')
        closest = None
        for level, price in fib_levels.items():
            dist = abs((trigger_price - price) / price) * 100
            if dist < min_dist:
                min_dist = dist
                closest = level

        if min_dist <= 2.0:
            score = 10 if min_dist <= 0.5 else 9 if min_dist <= 1.0 else 8
            if closest == '61.8%':
                score = min(10, score + 1)
            return score, f"Fib {closest} ({min_dist:.1f}%)"
        return 6, f"No Fib within 2% (closest: {closest} {min_dist:.1f}%)"

    except Exception:
        return 6, "Fib error"

def daily_scan(cache):
    """
    Daily scan - reads cache, fetches ONLY current prices, calculates signals.
    Fast! No monthly data download needed.
    """
    print(f"\n🎯 DAILY TRENDLINE SCAN")
    print(f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"📊 Scanning {len(cache['trendlines'])} cached trendlines...")
    print("-" * 60)

    trendlines = cache['trendlines']
    tickers = list(trendlines.keys())

    # Fetch ONLY current prices (fast - just today's data)
    print("📥 Fetching current prices...")
    current_prices = {}

    # Batch download current prices
    batch_size = 100
    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i+batch_size]
        try:
            data = yf.download(batch, period="2d", interval="1d",
                               auto_adjust=True, progress=False,
                               group_by='ticker')
            for ticker in batch:
                try:
                    if len(batch) == 1:
                        price = float(data['Close'].iloc[-1])
                    else:
                        price = float(data[ticker]['Close'].iloc[-1])
                    if price > 0:
                        current_prices[ticker] = price
                except Exception:
                    pass
        except Exception:
            pass

    print(f"✅ Got prices for {len(current_prices)} stocks")

    # Scan for signals
    critical_signals = []
    watchlist_signals = []

    for ticker, tl in trendlines.items():
        if ticker not in current_prices:
            continue

        current_price = current_prices[ticker]
        trigger_price = get_trigger_for_today(tl)

        if trigger_price <= 0:
            continue

        distance_pct = ((current_price - trigger_price) / trigger_price) * 100

        # Signal detection
        if abs(distance_pct) <= 2.0:
            confluence_score, confluence_note = get_fib_confluence(tl, trigger_price)
            stop_loss = trigger_price * 0.92
            target = trigger_price * 1.20

            signal = {
                'ticker': tl['ticker'],
                'current_price': round(current_price, 2),
                'trigger_price': trigger_price,
                'distance_pct': round(distance_pct, 2),
                'confluence_score': confluence_score,
                'confluence_note': confluence_note,
                'wick_touches': tl['wick_touch_count'],
                'stop_loss': round(stop_loss, 2),
                'target': round(target, 2),
                'status': 'CRITICAL' if abs(distance_pct) <= 1.0 else 'WATCHLIST'
            }

            if abs(distance_pct) <= 1.0:
                critical_signals.append(signal)
            else:
                watchlist_signals.append(signal)

        elif abs(distance_pct) <= 10.0:
            # Watchlist - approaching trendline
            watchlist_signals.append({
                'ticker': tl['ticker'],
                'current_price': round(current_price, 2),
                'trigger_price': trigger_price,
                'distance_pct': round(distance_pct, 2),
                'wick_touches': tl['wick_touch_count'],
                'status': 'APPROACHING'
            })

    # Sort by distance
    critical_signals.sort(key=lambda x: abs(x['distance_pct']))
    watchlist_signals.sort(key=lambda x: abs(x['distance_pct']))

    # Display results
    if critical_signals:
        print(f"\n🎯 CRITICAL ENTRY SIGNALS (±1%):")
        print(f"   {'Stock':12} {'Price':8} {'Trigger':8} {'Dist':6} {'Fib':5} {'Wicks':6} {'Stop':8} {'Target':8}")
        print(f"   {'-'*65}")
        for s in critical_signals:
            print(f"   {s['ticker']:12} ₹{s['current_price']:7.2f} ₹{s['trigger_price']:7.2f} {s['distance_pct']:+5.1f}% {s['confluence_score']:3}/10 {s['wick_touches']:4}  ₹{s['stop_loss']:7.2f} ₹{s['target']:7.2f}")

    if watchlist_signals[:10]:
        print(f"\n👀 WATCHLIST (±2% entry zone):")
        for s in [x for x in watchlist_signals if x['status'] == 'WATCHLIST'][:10]:
            print(f"   {s['ticker']:12} ₹{s['current_price']:7.2f} | Trigger: ₹{s['trigger_price']:7.2f} | {s['distance_pct']:+5.1f}% | Fib: {s['confluence_score']}/10")

    print(f"\n📊 SUMMARY:")
    print(f"   🎯 Critical signals: {len(critical_signals)}")
    print(f"   👀 Watchlist signals: {len([x for x in watchlist_signals if x['status'] == 'WATCHLIST'])}")
    print(f"   📈 Approaching (2-10%): {len([x for x in watchlist_signals if x['status'] == 'APPROACHING'])}")

    # Save to JSON for dashboard
    output = {
        'scan_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'cache_built': cache['built_at'],
        'total_scanned': len(current_prices),
        'critical_count': len(critical_signals),
        'watchlist_count': len(watchlist_signals),
        'critical_signals': critical_signals,
        'watchlist_signals': [x for x in watchlist_signals if x['status'] == 'WATCHLIST']
    }

    with open('trendline_screen.json', 'w') as f:
        json.dump(output, f, indent=2)

    print(f"\n💾 Results saved to: trendline_screen.json")
    return output

# ─── MAIN ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    # Load stock list (try multiple filenames for compatibility)
    stock_list_files = ['Stock List.csv', 'ind_nifty500list.csv', 'ind_nifty500list (1).csv']
    nifty_df = None
    
    for csv_file in stock_list_files:
        if os.path.exists(csv_file):
            try:
                nifty_df = pd.read_csv(csv_file)
                print(f"✅ Loaded stock list from: {csv_file}")
                break
            except Exception as e:
                print(f"⚠️ Failed to load {csv_file}: {e}")
                continue
    
    if nifty_df is None:
        print("❌ ERROR: No stock list CSV found. Tried:")
        for f in stock_list_files:
            print(f"   - {f}")
        sys.exit(1)
    
    tickers = [s + '.NS' for s in nifty_df['Symbol'].tolist()]

    force_rebuild = '--rebuild' in sys.argv

    # Step 1: Build/load cache (weekly rebuild only)
    cache = build_full_cache(tickers, force=force_rebuild)

    # Step 2: Daily scan using cache (fast!)
    daily_scan(cache)
