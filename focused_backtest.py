"""
Focused Backtest: ±10% tolerance | Target 20% & 25% | 1-5 Year windows
=======================================================================
Lists     : Nifty50 (50), Nifty500 (504), StockList (752)
Tolerance : 10% only
Targets   : 20%, 25%
Timeframes: 1Y, 2Y, 3Y, 4Y, 5Y
SL=8%, Timeout=180d, FibMin=7
Total     : 3 lists × 2 targets × 5 years = 30 combos
"""

import json, os
import pandas as pd
import numpy as np
from scipy.signal import argrelextrema
from datetime import datetime, timedelta
import yfinance as yf
import warnings
warnings.filterwarnings('ignore')

POSITION_SIZE  = 50000.0
SL_PCT         = 8.0
TOLERANCE      = 10.0
TARGETS        = [20.0, 25.0]
TIMEFRAMES     = [1, 2, 3, 4, 5]   # years
TIMEOUT_DAYS   = 180
CONFLUENCE_MIN = 7
WICK_TOL       = 5.0
MIN_TOUCHES    = 3

LISTS = {
    "Nifty50":   "../ind_nifty50list.csv",
    "Nifty500":  "../ind_nifty500list (1).csv",
    "StockList": "../Stock List.csv",
}


def load_tickers(path):
    try:
        df = pd.read_csv(path)
        return [str(s).strip() + ".NS" for s in df['Symbol'] if str(s).strip()]
    except Exception as e:
        print(f"  ERROR loading {path}: {e}")
        return []


def _scalar(val):
    if hasattr(val, 'iloc'):
        return float(val.iloc[0])
    if hasattr(val, '__len__') and not isinstance(val, str):
        return float(val.flat[0])
    return float(val)


def fit_trendline(mdf):
    if len(mdf) < 24:
        return None
    lows = mdf['Low'].values
    if lows.ndim > 1:
        lows = lows.flatten()
    lows = lows.astype(float)
    tb = argrelextrema(lows, np.less, order=10)
    for o in [8, 6, 5]:
        if len(tb[0]) >= 2:
            break
        tb = argrelextrema(lows, np.less, order=o)
    if len(tb[0]) < 2:
        return None
    n = min(3, len(tb[0]))
    ai = tb[0][-n:]
    x = [mdf['Price_Idx'].iloc[i] for i in ai]
    y = [lows[i] for i in ai]
    slope, intercept = np.polyfit(x, y, 1)
    if slope <= 0:
        return None
    wicks = sum(1 for i in range(len(mdf))
                if abs((lows[i] - (slope * mdf['Price_Idx'].iloc[i] + intercept))
                       / (slope * mdf['Price_Idx'].iloc[i] + intercept)) * 100 <= WICK_TOL)
    if wicks < MIN_TOUCHES:
        return None
    return slope, intercept, ai


def fib_score(lows, ai, trigger, mdf):
    lp = float(lows[ai[-1]])
    after = mdf.iloc[ai[-1]:]
    hv = after['High'].values
    if hv.ndim > 1:
        hv = hv.flatten()
    hv = hv.astype(float)
    mx = argrelextrema(hv, np.greater, order=3)[0]
    sh = float(hv[mx].max()) if len(mx) > 0 else float(hv.max())
    fr = sh - lp
    if fr <= 0:
        return 5
    lvls = {k: sh - fr * v for k, v in
            [('38.2', 0.382), ('50.0', 0.500), ('61.8', 0.618), ('78.6', 0.786), ('100', 1.0)]}
    md = min(abs((trigger - p) / p) * 100 for p in lvls.values())
    cl = min(lvls, key=lambda k: abs((trigger - lvls[k]) / lvls[k]) * 100)
    if md <= 1.5:
        s = 10 if md <= 0.3 else (9 if md <= 0.7 else 8)
        if cl == '61.8':
            s = min(10, s + 1)
    else:
        s = 5
    return s


def check_outcome(daily, entry_dt, ep, sp, tp, target_pct):
    fut = daily[daily.index > entry_dt]
    ddl = entry_dt + timedelta(days=TIMEOUT_DAYS)
    for dt, row in fut.iterrows():
        if dt > ddl:
            sub = fut[fut.index <= ddl]
            if sub.empty:
                return 'TIMEOUT', ddl, ep, TIMEOUT_DAYS, 0.0
            xp = _scalar(sub['Close'].iloc[-1])
            return 'TIMEOUT', sub.index[-1], xp, (sub.index[-1] - entry_dt).days, (xp - ep) / ep * 100
        if _scalar(row['Low']) <= sp:
            return 'STOP_LOSS', dt, sp, (dt - entry_dt).days, -SL_PCT
        if _scalar(row['High']) >= tp:
            return 'TARGET_HIT', dt, tp, (dt - entry_dt).days, target_pct
    lp = _scalar(fut['Close'].iloc[-1]) if not fut.empty else ep
    ld = (fut.index[-1] - entry_dt).days if not fut.empty else 0
    return 'OPEN', fut.index[-1] if not fut.empty else entry_dt, lp, ld, (lp - ep) / ep * 100


def scan(tickers, years, target_pct, data_cache):
    cutoff = datetime.now() - timedelta(days=years * 365)
    trades = []
    for ticker in tickers:
        mdf, daily = data_cache.get(ticker, (None, None))
        if mdf is None or daily is None or daily.empty:
            continue
        try:
            scan_months = mdf[mdf.index >= pd.Timestamp(cutoff)]
            seen = set()
            for bar_pos in range(len(scan_months)):
                gpos = mdf.index.get_loc(scan_months.index[bar_pos])
                hist = mdf.iloc[:gpos + 1].copy()
                hist['Price_Idx'] = np.arange(len(hist))
                res = fit_trendline(hist)
                if res is None:
                    continue
                slope, intercept, ai = res
                lv = hist['Low'].values
                if lv.ndim > 1:
                    lv = lv.flatten()
                lows = lv.astype(float)
                ci = float(hist['Price_Idx'].iloc[-1])
                cc = _scalar(hist['Close'].iloc[-1])
                trigger = slope * ci + intercept
                dist = (cc - trigger) / trigger * 100
                if abs(dist) > TOLERANCE:
                    continue
                fs = fib_score(lows, ai, trigger, hist)
                if fs < CONFLUENCE_MIN:
                    continue
                ed = scan_months.index[bar_pos].to_pydatetime()
                sk = f"{ticker}_{ed.strftime('%Y-%m')}"
                if sk in seen:
                    continue
                seen.add(sk)
                ep = round(trigger, 2)
                sp = round(ep * (1 - SL_PCT / 100), 2)
                tp = round(ep * (1 + target_pct / 100), 2)
                shares = int(POSITION_SIZE // ep)
                status = "CRITICAL" if abs(dist) <= 1.0 else "WATCHLIST"
                oc, xd, xp, hd, pp = check_outcome(daily, ed, ep, sp, tp, target_pct)
                trades.append({
                    "symbol":       ticker.replace(".NS", ""),
                    "entry_date":   ed.strftime("%Y-%m-%d"),
                    "exit_date":    xd.strftime("%Y-%m-%d") if hasattr(xd, 'strftime') else str(xd),
                    "entry_price":  ep,
                    "exit_price":   round(float(xp), 2),
                    "distance_pct": round(abs(dist), 2),
                    "status":       status,
                    "fib_score":    int(fs),
                    "outcome":      oc,
                    "holding_days": int(hd),
                    "pnl_pct":      round(float(pp), 2),
                    "pnl_amount":   round((float(xp) - ep) * shares, 2),
                })
        except Exception:
            continue
    return trades


def summarise(trades, list_name, years, target_pct):
    comp  = [t for t in trades if t['outcome'] in ('STOP_LOSS', 'TARGET_HIT', 'TIMEOUT')]
    wins  = [t for t in comp if t['pnl_pct'] > 0]
    loss  = [t for t in comp if t['pnl_pct'] <= 0]
    pnl   = round(sum(t['pnl_amount'] for t in trades), 2)
    wr    = round(len(wins) / len(comp) * 100, 1) if comp else 0
    pf_n  = sum(t['pnl_amount'] for t in wins)
    pf_d  = abs(sum(t['pnl_amount'] for t in loss))
    pf    = round(pf_n / pf_d, 2) if pf_d > 0 else (999.0 if pf_n > 0 else 0.0)
    crit  = [t for t in trades if t['status'] == 'CRITICAL']
    watch = [t for t in trades if t['status'] == 'WATCHLIST']

    def ss(lst):
        c = [t for t in lst if t['outcome'] in ('STOP_LOSS', 'TARGET_HIT', 'TIMEOUT')]
        w = [t for t in c if t['pnl_pct'] > 0]
        return {
            "count":    len(lst),
            "win_rate": round(len(w) / len(c) * 100, 1) if c else 0,
            "avg_pnl":  round(sum(t['pnl_pct'] for t in lst) / len(lst), 2) if lst else 0,
        }

    return {
        "list":          list_name,
        "years":         years,
        "target_pct":    target_pct,
        "total_signals": len(trades),
        "completed":     len(comp),
        "win_rate":      wr,
        "avg_return":    round(sum(t['pnl_pct'] for t in trades) / len(trades), 2) if trades else 0,
        "total_pnl":     pnl,
        "avg_pnl_trade": round(pnl / len(trades), 2) if trades else 0,
        "avg_hold_days": round(sum(t['holding_days'] for t in trades) / len(trades), 1) if trades else 0,
        "profit_factor": pf,
        "target_hits":   len([t for t in trades if t['outcome'] == 'TARGET_HIT']),
        "stop_losses":   len([t for t in trades if t['outcome'] == 'STOP_LOSS']),
        "timeouts":      len([t for t in trades if t['outcome'] == 'TIMEOUT']),
        "open_trades":   len([t for t in trades if t['outcome'] == 'OPEN']),
        "critical":      ss(crit),
        "watchlist":     ss(watch),
        "trades":        trades,
    }


def prefetch(tickers, name):
    cache = {}
    print(f"  Fetching {name} ({len(tickers)} tickers)...")
    for i, t in enumerate(tickers, 1):
        try:
            mdf = yf.download(t, period="8y", interval="1mo", auto_adjust=True, progress=False, timeout=20)
            if mdf.empty or len(mdf) < 24:
                cache[t] = (None, None)
                continue
            mdf = mdf.dropna()
            mdf['Price_Idx'] = np.arange(len(mdf))
            daily = yf.download(t, period="6y", interval="1d", auto_adjust=True, progress=False, timeout=20)
            cache[t] = (mdf, daily.dropna() if not daily.empty else pd.DataFrame())
        except Exception:
            cache[t] = (None, None)
        if i % 100 == 0:
            good = sum(1 for v in cache.values() if v[0] is not None)
            print(f"    {i}/{len(tickers)} ({good} valid)...")
    good = sum(1 for v in cache.values() if v[0] is not None)
    print(f"  Done: {good}/{len(tickers)} valid")
    return cache


def make_html(all_results, outpath):
    def cwr(v):  return '#22c55e' if v >= 65 else ('#eab308' if v >= 50 else '#ef4444')
    def cpf(v):  return '#22c55e' if v >= 3  else ('#eab308' if v >= 1.5 else '#ef4444')
    def cpnl(v): return '#22c55e' if v >= 0  else '#ef4444'

    list_names = list(LISTS.keys())
    tgt_colors = {20.0: '#60a5fa', 25.0: '#a78bfa'}

    # ── Per-list section: year vs target table ────────────────────────────
    list_sections = ''
    for ln in list_names:
        ticker_count = len(load_tickers(os.path.join(os.path.dirname(outpath), LISTS[ln])))
        rows = ''
        for yr in TIMEFRAMES:
            for tgt in TARGETS:
                key = f"{ln}|{yr}|T{tgt}"
                r = all_results.get(key)
                if not r:
                    continue
                best_row = ''
                rows += f"""
                <tr>
                  <td style="color:#60a5fa;font-weight:700">{yr}Y</td>
                  <td style="color:{tgt_colors[tgt]};font-weight:700">T{int(tgt)}%</td>
                  <td>{r['total_signals']}</td>
                  <td style="color:{cwr(r['win_rate'])};font-weight:700">{r['win_rate']}%</td>
                  <td style="color:{cpnl(r['avg_return'])}">{r['avg_return']}%</td>
                  <td style="color:{cpnl(r['total_pnl'])}">&#8377;{r['total_pnl']:,.0f}</td>
                  <td style="color:{cpf(r['profit_factor'])}">{r['profit_factor']}x</td>
                  <td>{r['avg_hold_days']}d</td>
                  <td style="color:#22c55e">{r['target_hits']}</td>
                  <td style="color:#ef4444">{r['stop_losses']}</td>
                  <td style="color:#eab308">{r['timeouts']}</td>
                  <td style="color:#64748b">{r['open_trades']}</td>
                  <td style="color:{cwr(r['critical']['win_rate'])}">{r['critical']['win_rate']}% ({r['critical']['count']})</td>
                  <td style="color:{cwr(r['watchlist']['win_rate'])}">{r['watchlist']['win_rate']}% ({r['watchlist']['count']})</td>
                </tr>"""

        list_sections += f"""
        <div class="card">
          <h2 class="card-title">&#128203; {ln} &#8212; {ticker_count} stocks &#124; Tolerance &#177;10%</h2>
          <div style="overflow-x:auto">
          <table class="tbl">
            <thead><tr>
              <th>Timeframe</th><th>Target</th><th>Signals</th><th>Win Rate</th><th>Avg Return</th>
              <th>Total P&amp;L</th><th>Profit Factor</th><th>Avg Hold</th>
              <th>&#127919;Hits</th><th>&#128721;SL</th><th>&#9200;Timeout</th><th>&#128194;Open</th>
              <th>CRITICAL WR</th><th>WATCHLIST WR</th>
            </tr></thead>
            <tbody>{rows}</tbody>
          </table>
          </div>
        </div>"""

    # ── Cross-list comparison: same year+target across 3 lists ───────────
    compare_sections = ''
    for tgt in TARGETS:
        for yr in TIMEFRAMES:
            compare_sections += f"""
            <tr>
              <td style="color:#60a5fa;font-weight:700">{yr}Y</td>
              <td style="color:{tgt_colors[tgt]};font-weight:700">T{int(tgt)}%</td>"""
            for ln in list_names:
                key = f"{ln}|{yr}|T{tgt}"
                r = all_results.get(key, {})
                if not r or r.get('total_signals', 0) == 0:
                    compare_sections += '<td colspan="4" style="color:#374151">no data</td>'
                    continue
                compare_sections += f"""
                  <td>{r['total_signals']}</td>
                  <td style="color:{cwr(r['win_rate'])};font-weight:700">{r['win_rate']}%</td>
                  <td style="color:{cpnl(r['total_pnl'])}">&#8377;{r['total_pnl']:,.0f}</td>
                  <td style="color:{cpf(r['profit_factor'])}">{r['profit_factor']}x</td>"""
            compare_sections += '</tr>'

    list_header_cols = ''.join(
        f'<th colspan="4" style="border-left:2px solid #2d3a4a">{ln}</th>'
        for ln in list_names
    )
    sub_header_cols = ''.join(
        '<th style="border-left:2px solid #2d3a4a">Signals</th><th>Win%</th><th>P&amp;L</th><th>PF</th>'
        for _ in list_names
    )

    # ── Trade detail tabs ─────────────────────────────────────────────────
    trade_tabs = ''
    trade_panels = ''
    for ln in list_names:
        for yr in TIMEFRAMES:
            for tgt in TARGETS:
                key    = f"{ln}|{yr}|T{tgt}"
                tab_id = key.replace('|', '_').replace(' ', '_').replace('.', '').replace('%', '')
                label  = f"{ln} {yr}Y T{int(tgt)}%"
                trade_tabs += f'<button class="tab-btn" onclick="showTrades(\'{tab_id}\')">{label}</button>'
                r = all_results.get(key, {})
                trades = r.get('trades', [])
                trows = ''
                for t in sorted(trades, key=lambda x: x['entry_date'], reverse=True):
                    oc_c  = '#22c55e' if t['outcome'] == 'TARGET_HIT' else ('#ef4444' if t['outcome'] == 'STOP_LOSS' else '#eab308')
                    pc    = '#22c55e' if t['pnl_pct'] >= 0 else '#ef4444'
                    sc    = '#ef4444' if t['status'] == 'CRITICAL' else '#eab308'
                    trows += f"""<tr>
                      <td style="font-weight:600">{t['symbol']}</td>
                      <td>{t['entry_date']}</td><td>{t['exit_date']}</td>
                      <td>&#8377;{t['entry_price']:,.2f}</td><td>&#8377;{t['exit_price']:,.2f}</td>
                      <td>{t['distance_pct']}%</td>
                      <td style="color:{sc}">{t['status']}</td>
                      <td>{t['fib_score']}</td>
                      <td style="color:{oc_c};font-weight:600">{t['outcome']}</td>
                      <td>{t['holding_days']}d</td>
                      <td style="color:{pc};font-weight:600">{t['pnl_pct']}%</td>
                      <td style="color:{pc}">&#8377;{t['pnl_amount']:,.0f}</td>
                    </tr>"""
                trade_panels += f"""
                <div id="trades_{tab_id}" class="trade-panel" style="display:none">
                  <p style="color:#64748b;font-size:12px;margin-bottom:8px">{len(trades)} trades | {ln} | {yr}Y lookback | Target {int(tgt)}%</p>
                  <div style="overflow-x:auto;max-height:480px;overflow-y:auto">
                  <table class="tbl">
                    <thead><tr>
                      <th>Symbol</th><th>Entry</th><th>Exit</th><th>Entry&#8377;</th><th>Exit&#8377;</th>
                      <th>Dist%</th><th>Status</th><th>Fib</th><th>Outcome</th><th>Hold</th>
                      <th>P&amp;L%</th><th>P&amp;L&#8377;</th>
                    </tr></thead>
                    <tbody>{trows}</tbody>
                  </table>
                  </div>
                </div>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trendline Backtest &#177;10% | T20% &amp; T25% | 1-5Y</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0a0e14;color:#e2e8f0;font-family:'Segoe UI',sans-serif;padding:20px}}
h1{{text-align:center;color:#10b981;font-size:24px;margin-bottom:4px}}
.sub{{text-align:center;color:#64748b;font-size:12px;margin-bottom:20px}}
.meta{{display:flex;gap:10px;justify-content:center;flex-wrap:wrap;margin-bottom:24px}}
.pill{{background:#1e2a38;border:1px solid #2d3a4a;padding:5px 14px;border-radius:20px;font-size:11px;color:#94a3b8}}
.pill span{{color:#10b981;font-weight:700}}
.card{{background:#111827;border:1px solid #1f2937;border-radius:12px;padding:20px;margin-bottom:20px}}
.card-title{{font-size:15px;font-weight:700;color:#f1f5f9;margin-bottom:12px}}
.tbl{{width:100%;border-collapse:collapse;font-size:11.5px}}
.tbl th{{background:#1e2a38;color:#94a3b8;padding:8px 11px;text-align:left;font-weight:600;border-bottom:1px solid #2d3a4a;white-space:nowrap}}
.tbl td{{padding:8px 11px;border-bottom:1px solid #1a2232;white-space:nowrap}}
.tbl tr:hover td{{background:#1a2535}}
.tab-row{{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px}}
.tab-btn{{background:#1e2a38;border:1px solid #2d3a4a;color:#94a3b8;padding:5px 12px;border-radius:16px;cursor:pointer;font-size:11px;transition:all 0.2s}}
.tab-btn:hover,.tab-btn.active{{background:#065f46;border-color:#10b981;color:#10b981}}
.trade-panel{{margin-top:6px}}
.best{{background:rgba(16,185,129,0.08);}}
</style>
</head>
<body>
<h1>&#128200; Trendline Backtest &#8212; &#177;10% | T20% &amp; T25% | 1&#8211;5 Year</h1>
<p class="sub">3 Lists &#215; 2 Targets &#215; 5 Timeframes = 30 Combos &#124; SL={SL_PCT}% &#124; FibMin={CONFLUENCE_MIN} &#124; Generated {datetime.now().strftime('%d %b %Y %H:%M')}</p>

<div class="meta">
  <div class="pill">Tolerance <span>&#177;10%</span></div>
  <div class="pill">Targets <span>20% &amp; 25%</span></div>
  <div class="pill">Timeframes <span>1Y / 2Y / 3Y / 4Y / 5Y</span></div>
  <div class="pill">SL <span>{SL_PCT}%</span></div>
  <div class="pill">Timeout <span>{TIMEOUT_DAYS}d</span></div>
  <div class="pill">Fib Min <span>{CONFLUENCE_MIN}</span></div>
  <div class="pill">Position <span>&#8377;{POSITION_SIZE:,.0f}</span></div>
</div>

{list_sections}

<div class="card">
  <h2 class="card-title">&#9878; Cross-List Comparison &#8212; Same Period &amp; Target</h2>
  <div style="overflow-x:auto">
  <table class="tbl">
    <thead>
      <tr>
        <th>Period</th><th>Target</th>
        {list_header_cols}
      </tr>
      <tr>
        <th></th><th></th>
        {sub_header_cols}
      </tr>
    </thead>
    <tbody>{compare_sections}</tbody>
  </table>
  </div>
</div>

<div class="card">
  <h2 class="card-title">&#128202; Trade Details</h2>
  <div class="tab-row">{trade_tabs}</div>
  {trade_panels}
</div>

<script>
function showTrades(id){{
  document.querySelectorAll('.trade-panel').forEach(p=>p.style.display='none');
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
  var el=document.getElementById('trades_'+id);
  if(el)el.style.display='block';
  event.target.classList.add('active');
}}
document.addEventListener('DOMContentLoaded',function(){{
  var first=document.querySelector('.tab-btn');
  if(first)first.click();
}});
</script>
</body>
</html>"""

    with open(outpath, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"HTML saved -> {outpath}")


if __name__ == "__main__":
    base = os.path.dirname(os.path.abspath(__file__))
    all_results = {}
    total = len(LISTS) * len(TARGETS) * len(TIMEFRAMES)
    combo = 0

    print(f"\n{'='*65}")
    print(f"  FOCUSED BACKTEST: {total} combos")
    print(f"  Tolerance=±{TOLERANCE}%  Targets={TARGETS}  Years={TIMEFRAMES}")
    print(f"{'='*65}")

    for list_name, csv_rel in LISTS.items():
        csv_path = os.path.join(base, csv_rel)
        tickers  = load_tickers(csv_path)
        if not tickers:
            continue
        print(f"\n{'='*65}")
        print(f"  {list_name}: {len(tickers)} tickers")
        print(f"{'='*65}")

        # Fetch data ONCE per list — reuse across all 10 combos
        cache = prefetch(tickers, list_name)

        for yr in TIMEFRAMES:
            for tgt in TARGETS:
                combo += 1
                key = f"{list_name}|{yr}|T{tgt}"
                print(f"  [{combo:02d}/{total}] {list_name} | {yr}Y | Target={tgt}%")
                trades  = scan(tickers, yr, tgt, cache)
                summary = summarise(trades, list_name, yr, tgt)
                all_results[key] = summary
                print(f"         -> {summary['total_signals']:3d} signals | "
                      f"WR {summary['win_rate']:5.1f}% | "
                      f"PF {summary['profit_factor']:5.2f}x | "
                      f"P&L Rs{summary['total_pnl']:>12,.0f}")

    # Save JSON
    out = {
        "generated_at": datetime.now().isoformat(),
        "config": {
            "tolerance": TOLERANCE, "sl_pct": SL_PCT,
            "targets": TARGETS, "timeframes": TIMEFRAMES,
            "timeout_days": TIMEOUT_DAYS, "confluence_min": CONFLUENCE_MIN,
        },
        "results": {k: {kk: vv for kk, vv in v.items() if kk != 'trades'} for k, v in all_results.items()},
        "trades":  {k: v.get('trades', []) for k, v in all_results.items()},
    }
    out_json = os.path.join(base, "focused_backtest_results.json")
    with open(out_json, 'w') as f:
        json.dump(out, f, indent=2)
    print(f"\nSaved -> {out_json}")

    out_html = os.path.join(base, "focused_backtest_report.html")
    make_html(all_results, out_html)
    print("Done.")
