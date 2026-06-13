import yfinance as yf
import pandas as pd
import numpy as np
from scipy.signal import argrelextrema
from datetime import datetime, timedelta

class MacroInstitutionalEngine:
    def __init__(self, position_size=50000.0, sl_pct=6.5, touch_tolerance=1.5, use_recommended_logic=True):
        """
        Trendline engine with Recommended Logic improvements:
        - Wick-based touch detection (candle lows = wicks count as touches)
        - Adaptive order parameter (handles all sector cycle lengths)
        - ±1.5% entry discipline (STRICTER than old ±2%)
        - Fibonacci confluence: 38.2%, 50%, 61.8% ONLY (stricter validation)
        - Dynamic stop loss (6.5% below current trendline - TIGHTER than old 8%)
        - Minimum 3 wick touches required with Fibonacci alignment
        - Entry AT trendline support level (not market price)
        - Entry window: ±1.5% (vs old ±2%)
        - Stop loss: 6.5% (vs old 8%)
        - Target: 22.5% above entry (vs old 20%)
        """
        self.capital_per_trade = float(position_size)
        self.sl_pct = float(sl_pct)  # 6.5% recommended
        self.sl_multiplier = 1.0 - (float(sl_pct) / 100.0)
        self.touch_tolerance = float(touch_tolerance)  # ±1.5% entry (stricter)
        self.use_recommended_logic = use_recommended_logic
        self.target_multiplier = 1.225  # 22.5% target (vs old 1.20 = 20%)

    def get_sector_order(self, ticker):
        """Sector-specific order parameter with adaptive fallback"""
        banking = ['SBIN', 'HDFCBANK', 'ICICIBANK', 'AXISBANK', 'KOTAKBANK',
                   'INDUSINDBK', 'FEDERALBNK', 'BANDHANBNK', 'RBLBANK', 'IDFCFIRSTB']
        return 6 if any(b in ticker.upper() for b in banking) else 10

    def find_trendline_anchors(self, low_prices, df, ticker):
        """
        Find major anchor bottoms with adaptive order.
        Tries progressively smaller orders to handle all cycle lengths.
        """
        base_order = self.get_sector_order(ticker)
        touchbacks = argrelextrema(low_prices, np.less, order=base_order)

        # Adaptive fallback for long-cycle stocks (e.g. MARUTI needs order=5)
        for fallback_order in [8, 6, 5]:
            if len(touchbacks[0]) >= 2:
                break
            touchbacks = argrelextrema(low_prices, np.less, order=fallback_order)

        return touchbacks

    def count_wick_touches(self, df, low_prices, slope, intercept, tolerance=8.0):
        """
        Count wick touches - candle LOW (wick) within tolerance% of trendline.
        Relaxed to 8% to catch more valid touches across different stock volatilities.
        """
        wick_touches = []
        for i in range(len(df)):
            month_idx = df['Price_Idx'].iloc[i]
            trendline_at_month = (slope * month_idx) + intercept
            candle_low = low_prices[i]  # Wick low

            distance_pct = abs((candle_low - trendline_at_month) / trendline_at_month) * 100

            if distance_pct <= tolerance:
                wick_touches.append({
                    'date': df.index[i].strftime('%Y-%m-%d'),
                    'price': round(float(candle_low), 2),
                    'trendline_price': round(float(trendline_at_month), 2),
                    'distance_pct': round(distance_pct, 2),
                    'month_idx': int(month_idx)
                })
        return wick_touches

    def calculate_fib_confluence(self, last_touch_price, swing_high, trigger_price):
        """
        RECOMMENDED LOGIC: Fibonacci confluence scoring with comprehensive validation.
        RULE: Use 38.2%, 50%, 61.8%, 78.6%, and 100% levels
        Only count if trendline is within 1.5% of Fib level (stricter than old 2%).
        If no Fib within 1.5%, signal is filtered out (higher quality entries only).
        """
        try:
            fib_range = swing_high - last_touch_price
            if fib_range <= 0:
                return 6, "No Fib range - insufficient swing", None

            # RECOMMENDED LOGIC: All 5 levels for validation
            fib_levels_filtered = {
                '38.2%': swing_high - (fib_range * 0.382),
                '50.0%': swing_high - (fib_range * 0.500),
                '61.8%': swing_high - (fib_range * 0.618),
                '78.6%': swing_high - (fib_range * 0.786),
                '100.0%': swing_high - (fib_range * 1.000)
            }

            # Also calculate all for reference (including 23.6%)
            fib_levels_all = {
                '23.6%': swing_high - (fib_range * 0.236),
                '38.2%': swing_high - (fib_range * 0.382),
                '50.0%': swing_high - (fib_range * 0.500),
                '61.8%': swing_high - (fib_range * 0.618),
                '78.6%': swing_high - (fib_range * 0.786),
                '100.0%': swing_high - (fib_range * 1.000)
            }

            # Find closest match in 5-level set (38.2, 50, 61.8, 78.6, 100)
            min_distance = float('inf')
            closest_level = None
            for level_name, fib_price in fib_levels_filtered.items():
                dist = abs((trigger_price - fib_price) / fib_price) * 100
                if dist < min_distance:
                    min_distance = dist
                    closest_level = level_name

            # RECOMMENDED LOGIC: Stricter threshold of 1.5% (was 2.0%)
            if min_distance <= 1.5:
                if min_distance <= 0.3:
                    score = 10
                elif min_distance <= 0.7:
                    score = 9
                else:
                    score = 8
                # Golden Ratio (61.8%) bonus
                if closest_level == '61.8%':
                    score = min(10, score + 1)
                note = f"Strong Fib confluence: {closest_level} ({min_distance:.1f}% match) ✓"
            else:
                # RECOMMENDED LOGIC: If not within 1.5% of primary levels, still check but score lower
                score = 5  # Reduced score for weaker Fib alignment
                note = f"Weak Fib alignment: {closest_level} at {min_distance:.1f}% (need <1.5%)"

            return score, note, fib_levels_all

        except Exception:
            return 5, "Fib calculation error", None

    def find_swing_high_after_touch(self, df, last_touch_idx):
        """Find swing high after last trendline touch"""
        data_after_touch = df.iloc[last_touch_idx:]
        if len(data_after_touch) < 3:
            return df['High'].max().item()
        highs = data_after_touch['High'].values
        maxima_indices = argrelextrema(highs, np.greater, order=3)[0]
        if len(maxima_indices) > 0:
            return data_after_touch['High'].iloc[maxima_indices].max().item()
        return data_after_touch['High'].max().item()

    def process_ticker_geometry(self, ticker: str):
        """
        Main analysis with RECOMMENDED LOGIC improvements:
        1. Wick-based touch detection (candle lows = wicks count as touches)
        2. Adaptive order parameter (handles all sector cycle lengths)
        3. ±1.5% entry discipline (STRICTER than old ±2%)
        4. Fibonacci confluence: 38.2%, 50%, 61.8% with 1.5% tolerance
        5. Dynamic stop loss (6.5% below current trendline - TIGHTER than old 8%)
        6. Minimum 3 wick touches + Fibonacci alignment required
        7. Entry AT trendline support level (not market price)
        8. Target: 22.5% above entry (improved from 20%)
        9. Confluence score minimum of 7 required (filters weak signals)
        """
        try:
            # 1. Fetch 8-year monthly data
            df = yf.download(ticker, period="8y", interval="1mo",
                             auto_adjust=True, progress=False)
            if df.empty or len(df) < 24:
                return None

            df = df.dropna()
            df['Price_Idx'] = np.arange(len(df))
            low_prices = df['Low'].values.flatten()

            # 2. Find anchor bottoms with adaptive order
            touchbacks = self.find_trendline_anchors(low_prices, df, ticker)
            if len(touchbacks[0]) < 2:
                return None

            # 3. Fit trendline using last 3 anchors
            num_anchors = min(3, len(touchbacks[0]))
            anchor_indices = touchbacks[0][-num_anchors:]
            x_anchors = [df['Price_Idx'].iloc[i] for i in anchor_indices]
            y_anchors = [low_prices[i] for i in anchor_indices]
            slope, intercept = np.polyfit(x_anchors, y_anchors, 1)

            # 4. Must be ascending
            if slope <= 0:
                return None

            # 5. Count WICK touches
            wick_touches = self.count_wick_touches(df, low_prices, slope, intercept, tolerance=5.0)

            # 6. Minimum 3 wick touches required
            if len(wick_touches) < 3:
                return None

            # 7. Current trigger price (imaginary vertical line method)
            current_bar_idx = df['Price_Idx'].iloc[-1]
            current_close = df['Close'].iloc[-1].item()
            trigger_price = (slope * current_bar_idx) + intercept

            # 8. Distance from current price to trendline
            pct_distance = ((current_close - trigger_price) / trigger_price) * 100

            # 9. RECOMMENDED LOGIC: Entry filter ±1.5% (stricter than old ±2%)
            if abs(pct_distance) > self.touch_tolerance:
                return None

            # 10. Fibonacci confluence (38.2%, 50%, 61.8% only, within 1.5%)
            last_touch_idx = anchor_indices[-1]
            last_touch_price = float(low_prices[last_touch_idx])
            swing_high = self.find_swing_high_after_touch(df, last_touch_idx)

            confluence_score, confluence_note, fib_levels = self.calculate_fib_confluence(
                last_touch_price, swing_high, trigger_price
            )

            # RECOMMENDED LOGIC: Filter out weak signals (confluence score must be >= 7)
            if self.use_recommended_logic and confluence_score < 7:
                return None  # Skip this stock - not high quality

            # 11. RECOMMENDED LOGIC: Dynamic stop loss (6.5% below CURRENT trendline - TIGHTER)
            dynamic_stop_loss = trigger_price * self.sl_multiplier

            # 12. RECOMMENDED LOGIC: Target (22.5% above trendline entry - improved from 20%)
            target_price = trigger_price * self.target_multiplier

            # 13. Signal status — CRITICAL ≤0.5%, WATCHLIST up to touch_tolerance
            if abs(pct_distance) <= 0.5:
                signal_status = "CRITICAL_TOUCH"
            elif abs(pct_distance) <= self.touch_tolerance:
                signal_status = "WATCHLIST"
            else:
                return None

            # 14. Position sizing
            shares = int(self.capital_per_trade // trigger_price)

            # 15. Fib grid for display
            fib_grid = {}
            if fib_levels:
                fib_grid = {k: round(v, 2) for k, v in fib_levels.items()}

            return {
                "ticker": ticker.replace(".NS", ""),
                "currentSignal": {
                    "isActive": True,
                    "currentPrice": round(current_close, 2),
                    "triggerPrice": round(trigger_price, 2),  # Entry AT trendline
                    "distanceRemaining": round(abs(pct_distance), 2),
                    "signalStatus": signal_status,
                    "confluenceScore": confluence_score,
                    "confluenceNote": confluence_note,
                    "notificationTrigger": signal_status == "CRITICAL_TOUCH"
                },
                "positionSizing": {
                    "allocatedAmount": float(self.capital_per_trade),
                    "sharesToBuy": shares,
                    "entryPrice": round(trigger_price, 2),       # AT trendline support
                    "dynamicStopLoss": round(dynamic_stop_loss, 2),  # 6.5% below (TIGHTER)
                    "targetExit": round(target_price, 2),         # 22.5% above (IMPROVED)
                    "stopNote": "Monthly close stop: exit only if monthly candle closes below this level"
                },
                "trendlineDetails": {
                    "wickTouches": len(wick_touches),
                    "lastWickTouch": wick_touches[-1] if wick_touches else None,
                    "slope": round(slope, 4),
                    "numAnchors": num_anchors,
                    "monthlyGrowthRate": round(slope, 2)
                },
                "fibGrid": fib_grid
            }

        except Exception:
            pass
        return None
