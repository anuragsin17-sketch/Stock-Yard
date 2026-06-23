#!/usr/bin/env python3
"""
Sync Volume Watchlist to DynamoDB
==================================
One-time script to sync the fresh volume_gainer_watchlist.json to DynamoDB.

Run this after manually updating the watchlist to ensure DynamoDB is in sync.
"""

import json
import sys

try:
    import dynamodb_helper as dh
except ImportError:
    print("❌ dynamodb_helper not found — ensure you're in backend_repo directory")
    sys.exit(1)

WATCHLIST_FILE = 'volume_gainer_watchlist.json'

def main():
    print("=" * 60)
    print("SYNC VOLUME WATCHLIST TO DYNAMODB")
    print("=" * 60)
    
    # Load watchlist
    try:
        with open(WATCHLIST_FILE) as f:
            data = json.load(f)
    except Exception as e:
        print(f"❌ Error reading {WATCHLIST_FILE}: {e}")
        return
    
    # Extract stocks array (support both formats)
    if isinstance(data, dict) and 'stocks' in data:
        stocks = data['stocks']
        last_scan = data.get('last_scan_run', {})
        print(f"\nLast scan: {last_scan.get('timestamp', 'unknown')}")
        print(f"Total stocks in watchlist: {len(stocks)}")
    elif isinstance(data, list):
        stocks = data
        print(f"\nTotal stocks in watchlist: {len(stocks)}")
    else:
        print(f"❌ Unexpected watchlist format")
        return
    
    if not stocks:
        print("⚠️  Watchlist is empty — nothing to sync")
        return
    
    # Sync to DynamoDB
    print(f"\n🔄 Syncing {len(stocks)} stocks to DynamoDB StockSignals table (VOLUME)...")
    try:
        dh.write_signals('VOLUME', stocks)
        print(f"\n✅ Successfully synced {len(stocks)} stocks to DynamoDB")
    except Exception as e:
        print(f"\n❌ DynamoDB sync failed: {e}")
        import traceback
        traceback.print_exc()
    
    print("=" * 60)

if __name__ == '__main__':
    main()
