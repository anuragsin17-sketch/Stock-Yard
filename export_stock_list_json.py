#!/usr/bin/env python3
"""
Export Stock List.csv to JSON for UI filtering
===============================================
Converts Stock List.csv to a JSON array of symbols
for use in the web UI to filter volume gainers.
"""

import csv
import json

CSV_FILE = 'Stock List.csv'
OUTPUT_FILE = 'stock_list.json'

def main():
    symbols = []
    
    with open(CSV_FILE, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            symbol = row.get('Symbol', '').strip().upper()
            if symbol:
                symbols.append(symbol)
    
    # Sort alphabetically
    symbols.sort()
    
    # Save as JSON
    with open(OUTPUT_FILE, 'w') as f:
        json.dump(symbols, f, indent=2)
    
    print(f"✅ Exported {len(symbols)} stocks to {OUTPUT_FILE}")

if __name__ == '__main__':
    main()
