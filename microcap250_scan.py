#!/usr/bin/env python3
"""
Nifty Microcap 250 Trendline Scanner
Same logic as nifty50_trendline_scan.py but for Microcap 250 stocks
"""

import json, time, os
import pandas as pd
from datetime import datetime
from geometric_engine import MacroInstitutionalEngine
from macro_fib_scanner import calculate_macro_fib


def load_microcap250_list():
    """Load Microcap 250 stock list from CSV."""
    csv_path = 'ind_niftymicrocap250_list.csv'
    if os.path.exists(csv_path):
        try:
            df = pd.read_csv(csv_path)
            syms = [str(s).strip() for s in df['Symbol'].tolist() if str(s).strip()]
            print(f"  Loaded {len(syms)} stocks from {csv_path}")
            return syms
        except Exception as e:
            print(f"  Error loading {csv_path}: {e}")
    print(f"  ERROR: {csv_path} not found")
    return []


def run_scan(write_to_json=True, position_size=50000.0):
    engine = MacroInstitutionalEngine(
        position_size=position_size,
        sl_pct=8.0,
        touch_tolerance=5.0,
        use_recommended_logic=True
    )

    print(f"\n{'='*70}")
    print(f"  MICROCAP 250 TRENDLINE + FIBONACCI SCAN")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  Score 10: Trendline only | Score 9: Fib only | Score 8: Both")
    print(f"  Rules: Post-2020 | Wick touch <=5% | Unbroken trendline")
    print(f"  Fib: 50%, 61.8%, 78.6% (March 2020 low to ATH)")
    print(f"{'='*70}\n")

    # Load trendline cache to get wickTouches for all stocks
    trendline_cache = {}
    if os.path.exists('trendline_cache.json'):
        try:
            with open('trendline_cache.json') as f:
                cache_data = json.load(f)
                trendline_cache = cache_data.get('trendlines', {})
            print(f"  Loaded trendline cache: {len(trendline_cache)} stocks with trendline data\n")
        except Exception as e:
            print(f"  Warning: Could not load trendline_cache.json: {e}\n")

    tickers = load_microcap250_list()
    if not tickers:
        print("ERROR: No stocks loaded. Exiting.")
        return [], {}

    results = []
    stats   = {'scanned': 0, 'found': 0, 'critical': 0, 'watchlist': 0, 'monitoring': 0, 
               'trendline_only': 0, 'fib_only': 0, 'confluence': 0}

    for i, ticker in enumerate(tickers, 1):
        ns_ticker = ticker + '.NS'
        try:
            # STEP 1: Check for TRENDLINE signal
            trendline_result = engine.process_ticker_geometry(ns_ticker)
            
            # STEP 2: Check for MACRO FIBONACCI signal
            fib_result = calculate_macro_fib(ticker)
            
            stats['scanned'] += 1
            
            # STEP 3: Merge results based on three-tier scoring
            has_trendline = trendline_result is not None
            has_fib = fib_result is not None
            
            if not has_trendline and not has_fib:
                continue  # No signal at all
            
            # Determine score and signal type
            if has_trendline and has_fib:
                final_score = 8
                signal_type = "CONFLUENCE"
                stats['confluence'] += 1
            elif has_trendline:
                final_score = 10
                signal_type = "TRENDLINE"
                stats['trendline_only'] += 1
            else:
                final_score = 9
                signal_type = "FIBONACCI"
                stats['fib_only'] += 1
            
            # Build result record
            if has_trendline:
                # Use trendline data as base
                sig    = trendline_result['currentSignal']
                sizing = trendline_result['positionSizing']
                tl     = trendline_result['trendlineDetails']
                fibs   = trendline_result.get('fibGrid', {})
                status = sig['signalStatus']
                
                # Get n_touches from cache if available
                n_touches = tl.get('wickTouches')
                if n_touches is None and ticker in trendline_cache:
                    n_touches = trendline_cache[ticker].get('n_touches')
                
                sig['confluenceScore'] = final_score
                if signal_type == "CONFLUENCE":
                    sig['confluenceNote'] = f"Confluence: Trendline + Fib {fib_result['fib_level']} ✓✓"
                else:
                    sig['confluenceNote'] = f"Trendline touch only"
                
                entry_record = {
                    'ticker':             ticker,
                    'currentPrice':       sig['currentPrice'],
                    'ema50':              None,
                    'ema200':             None,
                    'triggerPrice':       sizing['entryPrice'],
                    'distanceRemaining':  sig['distanceRemaining'],
                    'signalStatus':       status,
                    'notificationTrigger': sig['notificationTrigger'],
                    'confluenceScore':    final_score,
                    'patternZone':        sig['confluenceNote'],
                    'wickTouches':        n_touches,
                    'timeframe':          'monthly',
                    'signalType':         signal_type,
                    'week52High':         trendline_result.get('week52High'),
                    'week52Low':          trendline_result.get('week52Low'),
                    'positionSizing': {
                        'allocatedAmount': sizing['allocatedAmount'],
                        'sharesToBuy':     sizing['sharesToBuy'],
                        'entryPrice':      sizing['entryPrice'],
                        'strictStopLoss':  sizing['dynamicStopLoss'],
                        'pivotTargetExit': sizing['targetExit'],
                    },
                    'fibonacciLevels':    fibs,
                    'fibMatchLevel':      fib_result['fib_level'] if has_fib else None,
                    'fibMatchPrice':      fib_result['fib_price'] if has_fib else None,
                    'fibMatchDistancePct': fib_result['distance_pct'] if has_fib else None,
                    'trendlineSlope':     tl['slope'],
                    'anchor1Date':        tl.get('anchor1Date'),
                    'anchor2Date':        tl.get('anchor2Date'),
                }
                
            else:
                # Fibonacci-only signal
                status = "WATCHLIST"
                
                # Fetch from cache
                wick_touches = None
                trendline_slope = None
                anchor1_date = None
                anchor2_date = None

                if ticker in trendline_cache:
                    cache_tl = trendline_cache[ticker]
                    wick_touches = cache_tl.get('n_touches', None)
                    trendline_slope = cache_tl.get('slope', None)
                    anchor1_date = cache_tl.get('a1_date', None)
                    anchor2_date = cache_tl.get('a2_date', None)
                    if anchor1_date: anchor1_date = anchor1_date[:7]
                    if anchor2_date: anchor2_date = anchor2_date[:7]
                
                entry_record = {
                    'ticker':             ticker,
                    'currentPrice':       fib_result['currentPrice'],
                    'ema50':              None,
                    'ema200':             None,
                    'triggerPrice':       fib_result['fib_price'],
                    'distanceRemaining':  fib_result['distance_pct'],
                    'signalStatus':       status,
                    'notificationTrigger': False,
                    'confluenceScore':    final_score,
                    'patternZone':        f"Fib {fib_result['fib_level']} support",
                    'wickTouches':        wick_touches,
                    'timeframe':          'monthly',
                    'signalType':         signal_type,
                    'week52High':         None,
                    'week52Low':          None,
                    'positionSizing': {
                        'allocatedAmount': position_size,
                        'sharesToBuy':     max(1, int(position_size // fib_result['fib_price'])),
                        'entryPrice':      fib_result['fib_price'],
                        'strictStopLoss':  round(fib_result['fib_price'] * 0.92, 2),
                        'pivotTargetExit': round(fib_result['fib_price'] * 1.23, 2),
                    },
                    'fibonacciLevels':    fib_result['all_fib_levels'],
                    'fibMatchLevel':      fib_result['fib_level'],
                    'fibMatchPrice':      fib_result['fib_price'],
                    'fibMatchDistancePct': fib_result['distance_pct'],
                    'trendlineSlope':     trendline_slope,
                    'anchor1Date':        anchor1_date,
                    'anchor2Date':        anchor2_date,
                }
            
            results.append(entry_record)
            stats['found'] += 1
            if status == 'CRITICAL_TOUCH': stats['critical'] += 1
            elif status == 'WATCHLIST':    stats['watchlist'] += 1
            else:                          stats['monitoring'] += 1

            icon = '🎯' if status == 'CRITICAL_TOUCH' else '👀' if status == 'WATCHLIST' else '📊'
            type_icon = '🔀' if signal_type == 'CONFLUENCE' else '📈' if signal_type == 'TRENDLINE' else '📊'
            
            print(f"  {i:3d}. {ticker:<14} {icon}{type_icon} {status:<16} "
                  f"Entry:₹{entry_record['triggerPrice']:,.0f} "
                  f"Dist:{entry_record['distanceRemaining']:+.1f}% "
                  f"Score:{final_score}/10 [{signal_type}]")

        except Exception as e:
            stats['scanned'] += 1
            if 'NoneType' not in str(e):
                print(f"  Error processing {ticker}: {e}")

        time.sleep(0.2)

    # Sort: Score descending, then by distance
    results.sort(key=lambda x: (
        -x['confluenceScore'],
        x['distanceRemaining']
    ))

    print(f"\n{'='*70}")
    print(f"  Scanned: {stats['scanned']} | Signals: {stats['found']}")
    print(f"  Trendline-only (10): {stats['trendline_only']} | "
          f"Fib-only (9): {stats['fib_only']} | Confluence (8): {stats['confluence']}")
    print(f"  Critical: {stats['critical']} | Watchlist: {stats['watchlist']} | "
          f"Monitoring: {stats['monitoring']}")
    print(f"{'='*70}")

    if write_to_json and results:
        with open('microcap250_screen.json', 'w') as f:
            json.dump(results, f, indent=2)
        print(f"\n  Saved {len(results)} signals -> microcap250_screen.json")

    return results, stats


if __name__ == '__main__':
    import sys
    write = '--no-write' not in sys.argv
    run_scan(write_to_json=write)
