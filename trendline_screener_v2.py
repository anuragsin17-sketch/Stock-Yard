#!/usr/bin/env python3
"""
Trendline Screener v2 — Optimised Two-Phase Architecture
=========================================================
PHASE 1 — Weekly Cache Build (run once per week, ~30 min):
  python trendline_screener_v2.py --build
  - Downloads 10-year monthly data for all stocks
  - Fits best ascending trendline (post-2020, unbroken, >=3 touches)
  - Saves slope/intercept/metadata to trendline_cache.json
  - NO daily data download needed

PHASE 2 — Daily Signal Scan (run daily, ~2-3 min):
  python trendline_screener_v2.py --scan
  - Reads trendline_cache.json (no 10-year download)
  - Fetches ONLY current price (1-day data, batch download)
  - Checks if monthly LOW touches within 5% of trendline
  - Sends Telegram notifications for new touches
  - Writes trendline_screen.json for the frontend

GitHub Actions:
  - Sunday: run --build  (weekly rebuild)
  - Mon-Sat: run --scan  (daily check, fast)
"""

import json, os, sys, time, warnings
import pandas as pd
import numpy as np
from scipy.signal import argrelextrema
from datetime import datetime, timedelta
import yfinance as yf
import requests

warnings.filterwarnings('ignore')

# ─── CONFIG ──────────────────────────────────────────────────────────────────
POSITION_SIZE  = 50000.0
SL_PCT         = 8.0
TARGET_PCT     = 23.0
WICK_PCT       = 5.0
MIN_TOUCHES    = 3
POST_COVID     = '2020-04-01'
CACHE_FILE     = 'trendline_cache.json'
OUTPUT_FILE    = 'trendline_screen.json'
CACHE_MAX_DAYS = 7   # rebuild cache if older than 7 days

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT  = os.environ.get('TELEGRAM_CHAT_ID', '')
APP_URL        = 'https://anuragsin17-sketch.github.io/Stock-Yard-Public'


def _scalar(v):
    if hasattr(v,'iloc'): return float(v.iloc[0])
    if hasattr(v,'__len__') and not isinstance(v,str): return float(v.flat[0])
    return float(v)


def send_telegram(msg, buttons=None):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT: return
    try:
        payload = {'chat_id': TELEGRAM_CHAT, 'text': msg, 'parse_mode': 'Markdown'}
        if buttons: payload['reply_markup'] = buttons
        requests.post(f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
                      json=payload, timeout=10)
    except Exception: pass


def load_stocks():
    """Load stock list from CSV, fallback to Nifty 50."""
    for path in ['Stock List.csv', 'ind_nifty500list (1).csv', '../ind_nifty500list (1).csv']:
        if os.path.exists(path):
            try:
                df = pd.read_csv(path)
                syms = [str(s).strip() for s in df['Symbol'].tolist() if str(s).strip()]
                print(f"  Loaded {len(syms)} stocks from {path}")
                return syms
            except Exception: pass
    nifty50 = [
        'ADANIENT','ADANIPORTS','APOLLOHOSP','ASIANPAINT','AXISBANK',
        'BAJAJ-AUTO','BAJAJFINSV','BAJFINANCE','BHARTIARTL','BPCL',
        'BRITANNIA','CIPLA','COALINDIA','DIVISLAB','DRREDDY',
        'EICHERMOT','GRASIM','HCLTECH','HDFCBANK','HDFCLIFE',
        'HEROMOTOCO','HINDALCO','HINDUNILVR','ICICIBANK','INDUSINDBK',
        'INFY','ITC','JSWSTEEL','KOTAKBANK','LT',
        'M&M','MARUTI','NESTLEIND','NTPC','ONGC',
        'POWERGRID','RELIANCE','SBILIFE','SBIN','SHRIRAMFIN',
        'SUNPHARMA','TATACONSUM','TATAMOTORS','TATASTEEL','TCS',
        'TECHM','TITAN','ULTRACEMCO','WIPRO'
    ]
    print(f"  Using built-in Nifty 50 ({len(nifty50)} stocks)")
    return nifty50


# ─── PHASE 1: CACHE BUILD ────────────────────────────────────────────────────

def fit_trendline(data):
    """
    Fit best ascending trendline on post-COVID data.
    Returns (slope, intercept, a1_date, a2_date, n_touches) or None.
    """
    post = data[data.index >= pd.Timestamp(POST_COVID)].copy()
    if len(post) < 12: return None
    post['Idx'] = np.arange(len(post))

    lows   = post['Low'].values.flatten().astype(float)
    closes = post['Close'].values.flatten().astype(float)
    n      = len(post)

    anchors = set()
    for order in [10, 8, 6, 5, 4, 3]:
        for idx in argrelextrema(lows, np.less, order=order)[0]:
            anchors.add(int(idx))
    anchors = sorted(anchors)
    if len(anchors) < 2: return None

    best, best_score = None, -1

    for i in range(len(anchors)-1):
        a1 = anchors[i]
        if a1 > int(n*0.85): break
        for j in range(i+1, len(anchors)):
            a2 = anchors[j]
            if a2-a1 < 6: continue
            x = [float(post['Idx'].iloc[a1]), float(post['Idx'].iloc[a2])]
            y = [lows[a1], lows[a2]]
            slope, intercept = np.polyfit(x, y, 1)
            if slope <= 0: continue

            # Rule: no monthly close below trendline
            broken = False
            for k in range(n):
                tl = slope*float(post['Idx'].iloc[k])+intercept
                if tl > 0 and closes[k] < tl*0.98:
                    broken = True; break
            if broken: continue

            # Count wick touches
            touches = [k for k in range(n)
                       if (lambda tl: tl > 0 and abs((lows[k]-tl)/tl)*100 <= WICK_PCT)(
                           slope*float(post['Idx'].iloc[k])+intercept)]
            if len(touches) < MIN_TOUCHES: continue

            # Score
            tl_a1 = slope*float(post['Idx'].iloc[a1])+intercept
            tl_a2 = slope*float(post['Idx'].iloc[a2])+intercept
            acc   = 1.0/(1.0+abs((lows[a1]-tl_a1)/lows[a1])+abs((lows[a2]-tl_a2)/lows[a2]))
            score = len(touches)*15+(max(touches)/n)*25+acc*30+(a2-a1)*0.1

            if score > best_score:
                best_score = score
                best = {
                    'slope':      round(slope, 6),
                    'intercept':  round(intercept, 4),
                    'last_idx':   int(post['Idx'].iloc[-1]),
                    'last_date':  post.index[-1].strftime('%Y-%m-%d'),
                    'a1_date':    post.index[a1].strftime('%Y-%m-%d'),
                    'a2_date':    post.index[a2].strftime('%Y-%m-%d'),
                    'a1_price':   round(float(lows[a1]), 2),
                    'a2_price':   round(float(lows[a2]), 2),
                    'n_touches':  len(touches),
                    # Fib data for display
                    'fib_levels': _calc_fib(post, a2, lows),
                    'built_at':   datetime.now().strftime('%Y-%m-%d %H:%M'),
                }
    return best


def _calc_fib(post, a2, lows):
    """
    Fibonacci grid from 2020 COVID low to ATH (post-2020).
    0%   = 2020 low (COVID crash base)
    100% = ATH since 2020
    Retracement levels drawn from ATH downward:
      76.4% = ATH - range * 0.236
      61.8% = ATH - range * 0.382
      50.0% = ATH - range * 0.500
      38.2% = ATH - range * 0.618
      23.6% = ATH - range * 0.764
    """
    try:
        highs = post['High'].values.flatten().astype(float)
        lows_ = post['Low'].values.flatten().astype(float)
        dates = post.index

        # 2020 COVID low: minimum low in calendar year 2020
        mask_2020 = (dates >= pd.Timestamp('2020-01-01')) & (dates <= pd.Timestamp('2020-12-31'))
        lows_2020  = lows_[mask_2020]
        covid_low  = float(lows_2020.min()) if len(lows_2020) > 0 else float(lows_.min())

        # ATH: highest high from 2020 onwards
        mask_post  = dates >= pd.Timestamp('2020-01-01')
        highs_post = highs[mask_post]
        ath        = float(highs_post.max()) if len(highs_post) > 0 else float(highs.max())

        rng = ath - covid_low
        if rng <= 0:
            return {}

        # Retracement levels (drawn from ATH down to COVID low)
        fib = {
            '100.0%': round(ath, 2),           # ATH
            '76.4%':  round(ath - rng * 0.236, 2),
            '61.8%':  round(ath - rng * 0.382, 2),
            '50.0%':  round(ath - rng * 0.500, 2),
            '38.2%':  round(ath - rng * 0.618, 2),
            '23.6%':  round(ath - rng * 0.764, 2),
            '0.0%':   round(covid_low, 2),      # 2020 COVID low
        }

        # Extension levels (above ATH)
        fib['Ext_23.6%'] = round(ath + rng * 0.236, 2)
        fib['Ext_61.8%'] = round(ath + rng * 0.618, 2)

        def pocket(p):
            return round(p * 0.98, 2), round(p * 1.02, 2)

        p618lo, p618hi = pocket(fib['61.8%'])
        p500lo, p500hi = pocket(fib['50.0%'])
        p786lo, p786hi = pocket(fib['76.4%'])  # using 76.4 as "golden pocket" deep level
        p382lo, p382hi = pocket(fib['38.2%'])
        p100lo, p100hi = pocket(fib['100.0%'])

        return {
            '_weekly': fib,   # keep _weekly key for compatibility
            'ath':        round(ath, 2),
            'covid_low':  round(covid_low, 2),
            # Flat levels for frontend display
            '100.0%_W':   fib['100.0%'],
            '76.4%_W':    fib['76.4%'],
            '61.8%_W':    fib['61.8%'],
            '50.0%_W':    fib['50.0%'],
            '38.2%_W':    fib['38.2%'],
            '23.6%_W':    fib['23.6%'],
            '0.0%_W':     fib['0.0%'],
            'Ext_23.6%_W': fib['Ext_23.6%'],
            'Ext_61.8%_W': fib['Ext_61.8%'],
            # Pocket ranges (±2%)
            'pocket_618_low':  p618lo, 'pocket_618_high': p618hi,
            'pocket_500_low':  p500lo, 'pocket_500_high': p500hi,
            'pocket_786_low':  p786lo, 'pocket_786_high': p786hi,
            'pocket_382_low':  p382lo, 'pocket_382_high': p382hi,
            'pocket_100_low':  p100lo, 'pocket_100_high': p100hi,
        }
    except Exception:
        return {}


def build_cache(force=False):
    """
    Phase 1: Download 10-year monthly data for all stocks, fit trendlines.
    Saves cache to trendline_cache.json. Only runs weekly.
    """
    # Check if cache is fresh
    if not force and os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE) as f:
                existing = json.load(f)
            built = datetime.strptime(existing.get('built_at','2000-01-01'), '%Y-%m-%d %H:%M')
            age_days = (datetime.now() - built).days
            if age_days < CACHE_MAX_DAYS:
                print(f"Cache is fresh ({age_days}d old, max {CACHE_MAX_DAYS}d). Use --force to rebuild.")
                return existing
        except Exception: pass

    print(f"\n{'='*60}")
    print(f"  PHASE 1: BUILDING TRENDLINE CACHE")
    print(f"  This runs ONCE per week — downloading 10-year monthly data")
    print(f"{'='*60}\n")

    stocks = load_stocks()
    cache  = {}
    errors = []
    t_start = time.time()

    for i, sym in enumerate(stocks, 1):
        ticker = sym + '.NS'
        elapsed = time.time() - t_start
        eta = (elapsed/i)*(len(stocks)-i) if i > 3 else 0
        print(f"  [{i:3d}/{len(stocks)}] {sym:<14}", end='', flush=True)

        try:
            mdf = yf.download(ticker, period='10y', interval='1mo',
                              auto_adjust=True, progress=False, timeout=30)
            if isinstance(mdf.columns, pd.MultiIndex):
                mdf.columns = mdf.columns.get_level_values(0)
            mdf = mdf.dropna()

            result = fit_trendline(mdf)
            if result:
                cache[sym] = result
                print(f"-> TL found ({result['n_touches']} touches, "
                      f"{result['a1_date'][:7]}→{result['a2_date'][:7]})  "
                      f"ETA:{int(eta//60)}m{int(eta%60)}s")
            else:
                print(f"-> no trendline  ETA:{int(eta//60)}m{int(eta%60)}s")

        except Exception as e:
            print(f"-> ERROR")
            errors.append(sym)

        # Save partial every 50 stocks
        if i % 50 == 0:
            partial = {'built_at': datetime.now().strftime('%Y-%m-%d %H:%M'),
                       'trendlines': cache}
            with open(CACHE_FILE, 'w') as f: json.dump(partial, f, indent=2)
            print(f"\n  Partial save: {len(cache)} trendlines cached\n")

        time.sleep(0.15)

    # Final save
    output = {
        'built_at':   datetime.now().strftime('%Y-%m-%d %H:%M'),
        'total':      len(stocks),
        'found':      len(cache),
        'errors':     errors,
        'trendlines': cache,
    }
    with open(CACHE_FILE, 'w') as f: json.dump(output, f, indent=2)

    elapsed_total = time.time() - t_start
    print(f"\n  Cache built: {len(cache)}/{len(stocks)} trendlines")
    print(f"  Time: {int(elapsed_total//60)}m {int(elapsed_total%60)}s")
    print(f"  Saved -> {CACHE_FILE}")
    return output


# ─── PHASE 2: DAILY SCAN ─────────────────────────────────────────────────────

def get_trendline_price_today(tl):
    """
    Project trendline to today using cached slope/intercept.
    Adds months since last cache date to get current idx.
    NO data download needed — pure math.
    """
    last_date = datetime.strptime(tl['last_date'], '%Y-%m-%d')
    today     = datetime.now()
    months_elapsed = (today.year - last_date.year)*12 + (today.month - last_date.month)
    current_idx    = tl['last_idx'] + months_elapsed
    return round(tl['slope'] * current_idx + tl['intercept'], 2)


def fib_confluence(fib_levels, trigger_price, dist_to_trendline_pct=None):
    """
    Score 10 - price within +-2% of fib level (61.8/50/78.6/100%) AND TL <=3%
    Score  9 - price within +-1% of any fib level, no TL yet
    Score  8 - monitoring
    """
    near_tl = dist_to_trendline_pct is not None and dist_to_trendline_pct <= 3.0

    KEY_LEVELS = []
    if fib_levels:
        w_fib = fib_levels.get('_weekly', {})
        # Use new 2020-low → ATH levels; fall back to old keys
        for k in ['61.8%', '50.0%', '38.2%', '76.4%', '100.0%', '23.6%']:
            p = fib_levels.get(f'{k}_W') or w_fib.get(k, 0)
            if p > 0:
                KEY_LEVELS.append((k, p))

    # Find closest fib level and distance
    in_2pct  = None  # within +-2%
    in_1pct  = None  # within +-1%
    for lvl_name, lvl_price in KEY_LEVELS:
        d = abs((trigger_price - lvl_price) / lvl_price * 100)
        if d <= 2.0 and in_2pct is None:
            in_2pct = (lvl_name, lvl_price, round(d, 2))
        if d <= 1.0 and in_1pct is None:
            in_1pct = (lvl_name, lvl_price, round(d, 2))

    # Score 10: within +-2% of any fib level AND TL <=3%
    if in_2pct and near_tl:
        lvl_name, lvl_price, d = in_2pct
        return 10, f'{lvl_name} + TL ({dist_to_trendline_pct:.1f}%) +-{d:.1f}% ✓✓'

    # Score 9: within +-1% of any fib level (no TL yet)
    if in_1pct:
        lvl_name, lvl_price, d = in_1pct
        return 9, f'{lvl_name} Level (+-{d:.1f}%) — Watch for TL'

    # Score 8: monitoring
    d_str = f'{dist_to_trendline_pct:.1f}%' if dist_to_trendline_pct else '--'
    return 8, f'Monitoring (TL dist {d_str})'

def daily_scan(notify=True):
    """
    Phase 2: Fast daily scan using cached trendlines.
    Only downloads today's price (batch, very fast).
    """
    print(f"\n{'='*60}")
    print(f"  PHASE 2: DAILY TRENDLINE SCAN")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')} IST")
    print(f"{'='*60}\n")

    # Load cache
    if not os.path.exists(CACHE_FILE):
        print("  No cache found. Run --build first.")
        return
    with open(CACHE_FILE) as f:
        cache = json.load(f)
    trendlines = cache.get('trendlines', {})
    print(f"  Cache: {len(trendlines)} trendlines (built {cache.get('built_at','?')})")

    # Batch fetch current prices (all stocks in one call — very fast)
    print(f"\n  Fetching current prices (batch)...")
    symbols = list(trendlines.keys())
    tickers_ns = [s+'.NS' for s in symbols]

    current_prices = {}
    current_lows   = {}

    # Download last 5 days daily bars — use last valid close/low (handles after-hours NaN)
    batch_size = 100
    for i in range(0, len(tickers_ns), batch_size):
        batch = tickers_ns[i:i+batch_size]
        try:
            data = yf.download(batch, period='5d', interval='1d',
                               auto_adjust=True, progress=False,
                               group_by='ticker', timeout=30)
            for ticker in batch:
                sym = ticker.replace('.NS','')
                try:
                    if len(batch) == 1:
                        df_t = data[['Close','Low']].dropna()
                    else:
                        df_t = data[ticker][['Close','Low']].dropna()
                    if df_t.empty: continue
                    cp = float(df_t['Close'].iloc[-1])
                    cl = float(df_t['Low'].iloc[-1])
                    if cp > 0:
                        current_prices[sym] = cp
                        current_lows[sym]   = cl
                except Exception: pass
        except Exception as e:
            print(f"  Batch error: {e}")

    print(f"  Got prices for {len(current_prices)} stocks\n")

    # Check signals
    signals  = []
    prev_alerted = set()
    if os.path.exists('alerted_today.json'):
        try:
            with open('alerted_today.json') as f:
                alerted_data = json.load(f)
            # Reset if it's a new day
            if alerted_data.get('date') == datetime.now().strftime('%Y-%m-%d'):
                prev_alerted = set(alerted_data.get('symbols', []))
        except Exception: pass

    for sym, tl in trendlines.items():
        cp = current_prices.get(sym)
        cl = current_lows.get(sym)
        if not cp or not cl: continue

        tl_price = get_trendline_price_today(tl)
        if tl_price <= 0: continue

        # Signal: current LOW within 5% of trendline
        dist_low   = (cl - tl_price) / tl_price * 100
        dist_close = (cp - tl_price) / tl_price * 100

        if abs(dist_low) > 5.0: continue  # outside signal zone

        # Determine status
        if abs(dist_low) <= 1.0:   status = 'CRITICAL_TOUCH'
        elif abs(dist_low) <= 3.0: status = 'WATCHLIST'
        else:                      status = 'MONITORING'

        entry_price = round(tl_price, 2)
        stop_loss   = round(entry_price * (1 - SL_PCT/100), 2)
        target      = round(entry_price * (1 + TARGET_PCT/100), 2)
        shares      = max(1, int(POSITION_SIZE // entry_price))

        fib_levels = tl.get('fib_levels', {})
        fib_score, fib_note = fib_confluence(fib_levels, tl_price, abs(dist_low))

        # Find closest fib level for display (use weekly grid if available)
        fib_match = None
        fib_match_price = None
        if fib_levels:
            w_fib = fib_levels.get('_weekly', {})
            search_grid = w_fib if w_fib else fib_levels
            min_d = float('inf')
            for lvl, price in search_grid.items():
                if lvl.startswith('_') or lvl.startswith('Ext') or lvl == '0.0%': continue
                if not isinstance(price, (int, float)): continue
                d = abs((tl_price - price) / price) * 100
                if d < min_d: min_d, fib_match, fib_match_price = d, lvl, price

        signal = {
            'ticker':             sym,
            'currentPrice':       round(cp, 2),
            'currentLow':         round(cl, 2),
            'triggerPrice':       entry_price,
            'distanceRemaining':  round(abs(dist_low), 2),
            'signalStatus':       status,
            'notificationTrigger': status == 'CRITICAL_TOUCH',
            'confluenceScore':    fib_score,
            'patternZone':        fib_note,
            'wickTouches':        tl['n_touches'],
            'timeframe':          'monthly',
            'ema50':              None,
            'ema200':             None,
            'positionSizing': {
                'allocatedAmount': POSITION_SIZE,
                'sharesToBuy':     shares,
                'entryPrice':      entry_price,
                'strictStopLoss':  stop_loss,
                'pivotTargetExit': target,
            },
            'fibonacciLevels':    fib_levels,
            'fibMatchLevel':      fib_match,
            'fibMatchPrice':      fib_match_price,
            # Fibonacci levels (2020 low → ATH)
            'fib_levels':         fib_levels,
            'fib_ath':            fib_levels.get('ath'),
            'fib_covid_low':      fib_levels.get('covid_low'),
            'fib_100_weekly':     fib_levels.get('100.0%_W'),  # ATH
            'fib_618_weekly':     fib_levels.get('61.8%_W'),
            'fib_500_weekly':     fib_levels.get('50.0%_W'),
            'fib_382_weekly':     fib_levels.get('38.2%_W'),
            'fib_764_weekly':     fib_levels.get('76.4%_W'),
            'fib_236_weekly':     fib_levels.get('23.6%_W'),
            'fib_000_weekly':     fib_levels.get('0.0%_W'),    # 2020 low
            'fib_618_monthly':    None,
            'fib_500_monthly':    None,
            'fib_786_monthly':    None,
            'fib_100_monthly':    None,
            'fib_target_1':       fib_levels.get('Ext_23.6%_W'),
            'fib_target_2':       fib_levels.get('Ext_61.8%_W'),
            'trendlineSlope':     tl['slope'],
            'anchor1Date':        tl['a1_date'][:7],
            'anchor2Date':        tl['a2_date'][:7],
        }
        signals.append(signal)

        # Telegram notification for CRITICAL new touches
        # Telegram alert removed — Place Order monitor handles all entry/exit alerts
        # (prevents duplicate alerts from trendline screener + place_order_monitor)
        if notify and status == 'CRITICAL_TOUCH':
            prev_alerted.add(sym)  # still track to avoid re-alerting if re-enabled

    # Sort: CRITICAL first, then by distance
    signals.sort(key=lambda x: (
        0 if x['signalStatus']=='CRITICAL_TOUCH' else
        1 if x['signalStatus']=='WATCHLIST' else 2,
        x['distanceRemaining']
    ))

    # Save output
    with open(OUTPUT_FILE, 'w') as f: json.dump(signals, f, indent=2)

    # Save alerted list for today
    with open('alerted_today.json', 'w') as f:
        json.dump({'date': datetime.now().strftime('%Y-%m-%d'),
                   'symbols': list(prev_alerted)}, f)

    # Print summary
    critical = [s for s in signals if s['signalStatus']=='CRITICAL_TOUCH']
    watchlist = [s for s in signals if s['signalStatus']=='WATCHLIST']
    monitoring= [s for s in signals if s['signalStatus']=='MONITORING']

    print(f"  Signals: {len(signals)} total")
    print(f"    🎯 CRITICAL: {len(critical)}")
    print(f"    👀 WATCHLIST: {len(watchlist)}")
    print(f"    📊 MONITORING: {len(monitoring)}")

    if critical:
        print(f"\n  🎯 CRITICAL TOUCH:")
        for s in critical:
            print(f"    {s['ticker']:<14} ₹{s['triggerPrice']:>8,.0f}  dist:{s['distanceRemaining']:+.1f}%  score:{s['confluenceScore']}/10")
    if watchlist:
        print(f"\n  👀 WATCHLIST:")
        for s in watchlist:
            print(f"    {s['ticker']:<14} ₹{s['triggerPrice']:>8,.0f}  dist:{s['distanceRemaining']:+.1f}%")

    print(f"\n  Saved {len(signals)} signals -> {OUTPUT_FILE}")
    return signals


# ─── MAIN ────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    if '--build' in sys.argv:
        force = '--force' in sys.argv
        build_cache(force=force)

    elif '--scan' in sys.argv:
        no_notify = '--no-notify' in sys.argv
        signals = daily_scan(notify=not no_notify)

        # Copy output to frontend if running locally
        import shutil
        frontend_path = '../trendline_screen.json'
        if os.path.exists('../index.html'):
            shutil.copy(OUTPUT_FILE, frontend_path)
            print(f"  Copied -> {frontend_path}")

    elif '--auto' in sys.argv:
        # Smart: build if cache stale, then scan
        today = datetime.now()
        needs_build = True
        if os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE) as f: c = json.load(f)
                built = datetime.strptime(c.get('built_at','2000-01-01'), '%Y-%m-%d %H:%M')
                if (datetime.now()-built).days < CACHE_MAX_DAYS:
                    needs_build = False
            except Exception: pass

        if needs_build:
            print("Cache stale or missing — building...")
            build_cache(force=True)
        else:
            print("Cache fresh — skipping build")

        daily_scan(notify=True)

    else:
        print("Usage:")
        print("  python trendline_screener_v2.py --build         # Full rebuild (weekly)")
        print("  python trendline_screener_v2.py --build --force # Force rebuild")
        print("  python trendline_screener_v2.py --scan          # Fast daily scan")
        print("  python trendline_screener_v2.py --scan --no-notify  # Scan without Telegram")
        print("  python trendline_screener_v2.py --auto          # Smart: rebuild if stale, then scan")
