#!/usr/bin/env python3
"""
Nifty 50 + Stock List Trendline Scanner
Clean rules (validated by backtest — 62.3% WR, 2.71x PF):
  - Post-April 2020 data only
  - Signal when monthly LOW within 5% of ascending trendline
  - Trendline must have >= 3 wick touches & never broken (no close below)
  - Entry = trendline touch price | SL = 8% | Target = 23%

Three-tier scoring system:
  - Score 10: Trendline touch only
  - Score 9: Fibonacci level only (50%, 61.8%, 78.6% within ±5%)
  - Score 8: Dual confluence (Trendline + Fibonacci)
"""

import json, time, os
import pandas as pd
from datetime import datetime
from geometric_engine import MacroInstitutionalEngine
from macro_fib_scanner import calculate_macro_fib

# ─── STOCK LIST ──────────────────────────────────────────────────────────────
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


def load_stock_list():
    """Load full stock list from CSV, fallback to Nifty 50."""
    for csv_path in ['Stock List.csv', '../Stock List.csv', 'ind_nifty500list (1).csv']:
        if os.path.exists(csv_path):
            try:
                df = pd.read_csv(csv_path)
                syms = [str(s).strip() for s in df['Symbol'].tolist() if str(s).strip()]
                print(f"  Loaded {len(syms)} stocks from {csv_path}")
                return syms
            except Exception:
                pass
    print(f"  Using built-in Nifty 50 list ({len(NIFTY_50)} stocks)")
    return NIFTY_50


def run_scan(write_to_json=True, position_size=50000.0):
    engine = MacroInstitutionalEngine(
        position_size=position_size,
        sl_pct=8.0,
        touch_tolerance=5.0,
        use_recommended_logic=True
    )

    print(f"\n{'='*70}")
    print(f"  TRENDLINE + FIBONACCI SCAN  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  Score 10: Trendline only | Score 9: Fib only | Score 8: Both")
    print(f"  Rules: Post-2020 | Wick touch <=5% | Unbroken trendline")
    print(f"  Fib: 50%, 61.8%, 78.6% (March 2020 low to ATH)")
    print(f"{'='*70}\n")

    tickers = load_stock_list()
    results = []
    stats   = {'scanned': 0, 'found': 0, 'critical': 0, 'watchlist': 0, 'monitoring': 0, 
               'trendline_only': 0, 'fib_only': 0, 'confluence': 0}

    for i, ticker in enumerate(tickers, 1):
        ns_ticker = ticker + '.NS'
        try:
            # ──────────────────────────────────────────────────────────────
            # STEP 1: Check for TRENDLINE signal (existing logic - untouched)
            # ──────────────────────────────────────────────────────────────
            trendline_result = engine.process_ticker_geometry(ns_ticker)
            
            # ──────────────────────────────────────────────────────────────
            # STEP 2: Check for MACRO FIBONACCI signal (independent check)
            # ──────────────────────────────────────────────────────────────
            fib_result = calculate_macro_fib(ticker)
            
            stats['scanned'] += 1
            
            # ──────────────────────────────────────────────────────────────
            # STEP 3: Merge results based on three-tier scoring
            # ──────────────────────────────────────────────────────────────
            
            has_trendline = trendline_result is not None
            has_fib = fib_result is not None
            
            if not has_trendline and not has_fib:
                continue  # No signal at all
            
            # Determine score and signal type
            if has_trendline and has_fib:
                # Score 8: Dual confluence (both trendline + fib)
                final_score = 8
                signal_type = "CONFLUENCE"
                stats['confluence'] += 1
            elif has_trendline:
                # Score 10: Trendline only
                final_score = 10
                signal_type = "TRENDLINE"
                stats['trendline_only'] += 1
            else:
                # Score 9: Fibonacci only
                final_score = 9
                signal_type = "FIBONACCI"
                stats['fib_only'] += 1
            
            # ──────────────────────────────────────────────────────────────
            # Build result record
            # ──────────────────────────────────────────────────────────────
            
            if has_trendline:
                # Use trendline data as base
                sig    = trendline_result['currentSignal']
                sizing = trendline_result['positionSizing']
                tl     = trendline_result['trendlineDetails']
                fibs   = trendline_result.get('fibGrid', {})
                status = sig['signalStatus']
                
                # Override score with three-tier logic
                sig['confluenceScore'] = final_score
                if signal_type == "CONFLUENCE":
                    sig['confluenceNote'] = f"Confluence: Trendline + Fib {fib_result['fib_level']} ✓✓"
                else:
                    sig['confluenceNote'] = f"Trendline touch only"
                
                entry_record = {
                    # Core fields used by frontend
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
                    'wickTouches':        tl['wickTouches'],
                    'timeframe':          'monthly',
                    'signalType':         signal_type,
                    # 52W H/L
                    'week52High':         trendline_result.get('week52High'),
                    'week52Low':          trendline_result.get('week52Low'),
                    'positionSizing': {
                        'allocatedAmount': sizing['allocatedAmount'],
                        'sharesToBuy':     sizing['sharesToBuy'],
                        'entryPrice':      sizing['entryPrice'],
                        'strictStopLoss':  sizing['dynamicStopLoss'],
                        'pivotTargetExit': sizing['targetExit'],
                    },
                    # Fibonacci levels
                    'fibonacciLevels':    fibs,
                    'fibMatchLevel':      fib_result['fib_level'] if has_fib else None,
                    'fibMatchPrice':      fib_result['fib_price'] if has_fib else None,
                    'fibMatchDistancePct': fib_result['distance_pct'] if has_fib else None,
                    # Trendline metadata
                    'trendlineSlope':     tl['slope'],
                    'anchor1Date':        tl.get('anchor1Date'),
                    'anchor2Date':        tl.get('anchor2Date'),
                }
                
            else:
                # Fibonacci-only signal (no trendline)
                # Build record from fib_result
                status = "WATCHLIST"  # Fib-only signals are watchlist by default
                
                entry_record = {
                    # Core fields
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
                    'wickTouches':        None,
                    'timeframe':          'monthly',
                    'signalType':         signal_type,
                    # 52W H/L (not available for fib-only, set to None)
                    'week52High':         None,
                    'week52Low':          None,
                    'positionSizing': {
                        'allocatedAmount': position_size,
                        'sharesToBuy':     max(1, int(position_size // fib_result['fib_price'])),
                        'entryPrice':      fib_result['fib_price'],
                        'strictStopLoss':  round(fib_result['fib_price'] * 0.92, 2),  # 8% SL
                        'pivotTargetExit': round(fib_result['fib_price'] * 1.23, 2),  # 23% target
                    },
                    # Fibonacci levels
                    'fibonacciLevels':    fib_result['all_fib_levels'],
                    'fibMatchLevel':      fib_result['fib_level'],
                    'fibMatchPrice':      fib_result['fib_price'],
                    'fibMatchDistancePct': fib_result['distance_pct'],
                    # Trendline metadata (none for fib-only)
                    'trendlineSlope':     None,
                    'anchor1Date':        None,
                    'anchor2Date':        None,
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
            # Only log if it's not a common data issue
            if 'NoneType' not in str(e):
                print(f"  Error processing {ticker}: {e}")

        time.sleep(0.2)

    # Sort: Score descending (10 → 9 → 8), then by distance
    results.sort(key=lambda x: (
        -x['confluenceScore'],  # Higher score first
        x['distanceRemaining']   # Then by distance ascending
    ))

    print(f"\n{'='*70}")
    print(f"  Scanned: {stats['scanned']} | Signals: {stats['found']}")
    print(f"  Trendline-only (10): {stats['trendline_only']} | "
          f"Fib-only (9): {stats['fib_only']} | Confluence (8): {stats['confluence']}")
    print(f"  Critical: {stats['critical']} | Watchlist: {stats['watchlist']} | "
          f"Monitoring: {stats['monitoring']}")
    print(f"{'='*70}")

    if write_to_json and results:
        with open('trendline_screen.json', 'w') as f:
            json.dump(results, f, indent=2)
        print(f"\n  Saved {len(results)} signals -> trendline_screen.json")

    return results, stats


if __name__ == '__main__':
    import sys
    write = '--no-write' not in sys.argv
    run_scan(write_to_json=write)
