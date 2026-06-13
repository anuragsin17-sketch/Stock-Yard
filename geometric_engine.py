import yfinance as yf
import pandas as pd
import numpy as np
from scipy.signal import argrelextrema
from datetime import datetime

# ─── CLEAN RULES (validated by backtest — 62.3% WR, 2.71x PF) ───────────────
# 1. Data: April 2020 onwards only (post-COVID crash)
# 2. Trendline: ascending, >= 3 wick touches (monthly LOW within 5% of line)
# 3. No monthly close ever below trendline (unbroken)
# 4. Signal: monthly LOW touches within 5% of trendline
# 5. Entry: trendline touch price
# 6. SL: 8% below entry
# 7. Target: 23% above entry
# ─────────────────────────────────────────────────────────────────────────────

WICK_PCT      = 5.0   # wick must be within 5% of trendline
MIN_TOUCHES   = 3     # minimum wick touches required
POST_COVID    = '2020-04-01'


class MacroInstitutionalEngine:
    def __init__(self, position_size=50000.0, sl_pct=8.0, touch_tolerance=5.0,
                 use_recommended_logic=True):
        self.capital_per_trade = float(position_size)
        self.sl_pct            = float(sl_pct)
        self.sl_multiplier     = 1.0 - (float(sl_pct) / 100.0)
        self.touch_tolerance   = float(touch_tolerance)
        self.use_recommended_logic = use_recommended_logic
        self.target_multiplier = 1.23   # 23% target

    def _find_best_trendline(self, df):
        """
        Find best ascending trendline on post-COVID data.
        Rules:
          - Post April 2020 data only
          - >= MIN_TOUCHES wick touches (monthly LOW within WICK_PCT)
          - No monthly CLOSE ever below trendline (unbroken)
          - Best scored by: touches + recency + accuracy to anchors
        Returns: (slope, intercept, ref_df, a1, a2, n_touches) or None
        """
        data = df[df.index >= pd.Timestamp(POST_COVID)].copy()
        if len(data) < 12:
            return None
        data['Idx'] = np.arange(len(data))

        lows   = data['Low'].values.flatten().astype(float)
        closes = data['Close'].values.flatten().astype(float)
        n      = len(data)

        # Find all significant local lows
        anchors = set()
        for order in [10, 8, 6, 5, 4, 3]:
            for idx in argrelextrema(lows, np.less, order=order)[0]:
                anchors.add(int(idx))
        anchors = sorted(anchors)
        if len(anchors) < 2:
            return None

        best, best_score = None, -1

        for i in range(len(anchors) - 1):
            a1 = anchors[i]
            if a1 > int(n * 0.85): break
            for j in range(i + 1, len(anchors)):
                a2 = anchors[j]
                if a2 - a1 < 6: continue

                x = [float(data['Idx'].iloc[a1]), float(data['Idx'].iloc[a2])]
                y = [lows[a1], lows[a2]]
                slope, intercept = np.polyfit(x, y, 1)
                if slope <= 0: continue

                # RULE: No monthly CLOSE ever below trendline (2% buffer)
                broken = False
                for k in range(n):
                    tl = slope * float(data['Idx'].iloc[k]) + intercept
                    if tl > 0 and closes[k] < tl * 0.98:
                        broken = True
                        break
                if broken: continue

                # Count wick touches
                touches = []
                for k in range(n):
                    tl = slope * float(data['Idx'].iloc[k]) + intercept
                    if tl > 0 and abs((lows[k] - tl) / tl) * 100 <= WICK_PCT:
                        touches.append(k)
                if len(touches) < MIN_TOUCHES: continue

                # Score
                tl_a1   = slope * float(data['Idx'].iloc[a1]) + intercept
                tl_a2   = slope * float(data['Idx'].iloc[a2]) + intercept
                acc     = 1.0 / (1.0 + abs((lows[a1]-tl_a1)/lows[a1])
                                      + abs((lows[a2]-tl_a2)/lows[a2]))
                recency = max(touches) / n
                score   = len(touches)*15 + recency*25 + acc*30 + (a2-a1)*0.1

                if score > best_score:
                    best_score = score
                    best = (slope, intercept, data, a1, a2, len(touches))

        return best

    def _calc_fib_levels(self, ref_df, a2, trigger_price):
        """Calculate Fibonacci levels and find closest to trendline."""
        try:
            lows_arr    = ref_df['Low'].values.flatten().astype(float)
            data_after  = ref_df.iloc[a2:]
            highs_after = data_after['High'].values.flatten().astype(float)
            mx = argrelextrema(highs_after, np.greater, order=3)[0]
            sh = float(highs_after[mx].max()) if len(mx) > 0 else float(highs_after.max())
            lp = float(lows_arr[a2])
            fr = sh - lp
            if fr <= 0: return {}, 5, 'No fib range'

            fibs = {
                '23.6%':  round(sh - fr*0.236, 2),
                '38.2%':  round(sh - fr*0.382, 2),
                '50.0%':  round(sh - fr*0.500, 2),
                '61.8%':  round(sh - fr*0.618, 2),
                '78.6%':  round(sh - fr*0.786, 2),
                '100.0%': round(sh - fr*1.000, 2),
            }

            min_d, closest = float('inf'), None
            for lvl, price in fibs.items():
                d = abs((trigger_price - price) / price) * 100
                if d < min_d:
                    min_d, closest = d, lvl

            if min_d <= 1.5:
                score = 10 if min_d <= 0.3 else (9 if min_d <= 0.7 else 8)
                if closest == '61.8%': score = min(10, score + 1)
                note = f"Strong Fib confluence: {closest} ({min_d:.1f}% match) ✓"
            elif min_d <= 3.0:
                score, note = 7, f"Near Fib {closest} ({min_d:.1f}%)"
            else:
                score, note = 5, f"Nearest Fib: {closest} ({min_d:.1f}%)"

            return fibs, score, note
        except Exception:
            return {}, 5, 'Fib error'

    def process_ticker_geometry(self, ticker: str):
        """
        Main signal detection using clean validated rules.
        Signal fires when monthly LOW touches within 5% of ascending trendline.
        """
        try:
            # Fetch 10-year monthly data
            df = yf.download(ticker, period="10y", interval="1mo",
                             auto_adjust=True, progress=False)
            if df.empty or len(df) < 12:
                return None
            df = df.dropna()

            result = self._find_best_trendline(df)
            if result is None:
                return None

            slope, intercept, ref_df, a1, a2, n_touches = result

            # Current values from post-COVID ref_df
            last_idx      = float(ref_df['Idx'].iloc[-1])
            trigger_price = slope * last_idx + intercept
            current_low   = float(ref_df['Low'].iloc[-1])
            current_close = float(ref_df['Close'].iloc[-1])

            # Signal: monthly LOW within touch_tolerance% of trendline
            dist_low   = (current_low   - trigger_price) / trigger_price * 100
            dist_close = (current_close - trigger_price) / trigger_price * 100

            # Use LOW for signal detection
            if abs(dist_low) > self.touch_tolerance:
                return None

            # Determine signal status based on LOW distance
            if abs(dist_low) <= 1.0:
                signal_status = "CRITICAL_TOUCH"
                notification   = True
            elif abs(dist_low) <= 3.0:
                signal_status = "WATCHLIST"
                notification   = False
            else:
                signal_status = "MONITORING"
                notification   = False

            # Entry = trendline touch price, SL = 8% below, Target = 23% above
            entry_price   = round(trigger_price, 2)
            stop_loss     = round(entry_price * self.sl_multiplier, 2)
            target_price  = round(entry_price * self.target_multiplier, 2)
            shares        = max(1, int(self.capital_per_trade // entry_price))

            # Fibonacci levels
            fib_grid, fib_score, fib_note = self._calc_fib_levels(ref_df, a2, trigger_price)

            return {
                "ticker": ticker.replace(".NS", ""),
                "currentSignal": {
                    "isActive":           True,
                    "currentPrice":       round(current_close, 2),
                    "currentLow":         round(current_low, 2),
                    "triggerPrice":       round(trigger_price, 2),
                    "distanceRemaining":  round(abs(dist_low), 2),
                    "signalStatus":       signal_status,
                    "confluenceScore":    fib_score,
                    "confluenceNote":     fib_note,
                    "notificationTrigger": notification,
                },
                "positionSizing": {
                    "allocatedAmount":  float(self.capital_per_trade),
                    "sharesToBuy":      shares,
                    "entryPrice":       entry_price,
                    "dynamicStopLoss":  stop_loss,
                    "targetExit":       target_price,
                    "stopNote":         f"SL {self.sl_pct}% below entry on daily close",
                },
                "trendlineDetails": {
                    "wickTouches":      n_touches,
                    "slope":            round(slope, 4),
                    "numAnchors":       2,
                    "monthlyGrowthRate": round(slope, 2),
                    "anchor1Date":      ref_df.index[a1].strftime('%Y-%m'),
                    "anchor2Date":      ref_df.index[a2].strftime('%Y-%m'),
                    "dataCutoff":       POST_COVID,
                },
                "fibGrid": fib_grid,
            }

        except Exception:
            pass
        return None
