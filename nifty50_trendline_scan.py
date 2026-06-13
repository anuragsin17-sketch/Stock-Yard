#!/usr/bin/env python3
"""
Nifty 50 + Stock List Trendline Scanner
Clean rules (validated by backtest — 62.3% WR, 2.71x PF):
  - Post-April 2020 data only
  - Signal when monthly LOW within 5% of ascending trendline
  - Trendline must have >= 3 wick touches & never broken (no close below)
  - Entry = trendline touch price | SL = 8% | Target = 23%
"""

import json, time, os
import pandas as pd
from datetime import datetime
from geometric_engine import MacroInstitutionalEngine

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

    print(f"\n{'='*65}")
    print(f"  TRENDLINE SCAN  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  Rules: Post-2020 | Wick touch <=5% | Unbroken trendline")
    print(f"  Entry: trendline price | SL: 8% | Target: 23%")
    print(f"{'='*65}\n")

    tickers = load_stock_list()
    results = []
    stats   = {'scanned': 0, 'found': 0, 'critical': 0, 'watchlist': 0, 'monitoring': 0}

    for i, ticker in enumerate(tickers, 1):
        ns_ticker = ticker + '.NS'
        try:
            result = engine.process_ticker_geometry(ns_ticker)
            stats['scanned'] += 1

            if result:
                sig    = result['currentSignal']
                sizing = result['positionSizing']
                tl     = result['trendlineDetails']
                fibs   = result.get('fibGrid', {})
                status = sig['signalStatus']

                # Find closest fib to trendline
                fib_match = None
                fib_match_price = None
                fib_match_dist  = None
                if fibs:
                    min_d = float('inf')
                    for lvl, price in fibs.items():
                        d = abs((sizing['entryPrice'] - price) / price * 100)
                        if d < min_d:
                            min_d = d
                            fib_match = lvl
                            fib_match_price = price
                            fib_match_dist  = round(d, 2)

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
                    'confluenceScore':    sig['confluenceScore'],
                    'patternZone':        sig['confluenceNote'],
                    'wickTouches':        tl['wickTouches'],
                    'timeframe':          'monthly',
                    'positionSizing': {
                        'allocatedAmount': sizing['allocatedAmount'],
                        'sharesToBuy':     sizing['sharesToBuy'],
                        'entryPrice':      sizing['entryPrice'],
                        'strictStopLoss':  sizing['dynamicStopLoss'],
                        'pivotTargetExit': sizing['targetExit'],
                    },
                    # Fibonacci levels for frontend display
                    'fibonacciLevels':    fibs,
                    'fibMatchLevel':      fib_match,
                    'fibMatchPrice':      fib_match_price,
                    'fibMatchDistancePct': fib_match_dist,
                    # Trendline metadata
                    'trendlineSlope':     tl['slope'],
                    'anchor1Date':        tl.get('anchor1Date'),
                    'anchor2Date':        tl.get('anchor2Date'),
                }
                results.append(entry_record)
                stats['found'] += 1
                if status == 'CRITICAL_TOUCH': stats['critical'] += 1
                elif status == 'WATCHLIST':    stats['watchlist'] += 1
                else:                          stats['monitoring'] += 1

                icon = '🎯' if status == 'CRITICAL_TOUCH' else '👀' if status == 'WATCHLIST' else '📊'
                fib_info = f"Fib:{fib_match}({fib_match_dist:.1f}%)" if fib_match else "No Fib"
                print(f"  {i:3d}. {ticker:<14} {icon} {status:<16} "
                      f"TL:₹{sizing['entryPrice']:,.0f} "
                      f"Dist:{sig['distanceRemaining']:+.1f}% "
                      f"Score:{sig['confluenceScore']}/10 {fib_info}")
            else:
                pass  # silent for no-signal stocks

        except Exception as e:
            stats['scanned'] += 1

        time.sleep(0.2)

    # Sort: CRITICAL first, then WATCHLIST, then MONITORING, then by distance
    results.sort(key=lambda x: (
        0 if x['signalStatus'] == 'CRITICAL_TOUCH' else
        1 if x['signalStatus'] == 'WATCHLIST' else 2,
        x['distanceRemaining']
    ))

    print(f"\n{'='*65}")
    print(f"  Scanned: {stats['scanned']} | Signals: {stats['found']} | "
          f"Critical: {stats['critical']} | Watchlist: {stats['watchlist']} | "
          f"Monitoring: {stats['monitoring']}")
    print(f"{'='*65}")

    if write_to_json and results:
        with open('trendline_screen.json', 'w') as f:
            json.dump(results, f, indent=2)
        print(f"\n  Saved {len(results)} signals -> trendline_screen.json")

    return results, stats


if __name__ == '__main__':
    import sys
    write = '--no-write' not in sys.argv
    run_scan(write_to_json=write)
