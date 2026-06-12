# Stock Yard — Architecture & Developer Guide

**Last updated:** June 2026  
**Author:** Kiro AI + Anurag Singh

---

## Overview

Stock Yard is an institutional-grade stock screening and trading dashboard for NSE (India). It uses a **two-repo architecture** to separate the frontend UI from the backend data pipeline.

```
User opens dashboard (GitHub Pages)
    ↓
index.html fetches JSON data files
    ↓
Displays live trendline signals, radar trades, LTP prices
    ↓
User clicks "Take Trade" or "Add Trade"
    ↓
Order placed via EC2 API (Angel One)
    ↓
Backend workflow syncs radar_trades.json back to Public repo
```

---

## Repository Structure

### 1. `Stock-Yard-Public` (PUBLIC)
**GitHub:** `https://github.com/anuragsin17-sketch/Stock-Yard-Public`  
**GitHub Pages:** `https://anuragsin17-sketch.github.io/Stock-Yard-Public/`

**Purpose:** Frontend UI only. Serves the dashboard via GitHub Pages.

**Files:**
```
index.html              ← Entire dashboard UI (single file app)
Stock List.csv          ← NIFTY500 stock list for symbol search
data.json               ← Volume breakout stocks (written by backend)
trendline_screen.json   ← Trendline signals (written by backend)
radar_trades.json       ← Active trades (written by backend)
.github/workflows/
    pages.yml           ← GitHub Pages deployment (runs on every push to main)
README.md
```

**Rule: Nothing in this repo should run code or workflows that modify `index.html`.**  
Only humans push `index.html`. Backend workflows push only JSON data files.

---

### 2. `Stock-Yard` (PRIVATE)
**GitHub:** `https://github.com/anuragsin17-sketch/Stock-Yard`  
**Local:** `d:\Stock Yard Backend\`

**Purpose:** Backend data pipeline, stock screener, trade execution.

**Key files:**
```
update_feed.py                  ← Trendline scanner (main screener)
angel_monitor.py                ← Trade monitor (entry/exit detection)
angel_order_handler.py          ← EC2 Flask API (Angel One integration)
angel_trade.py                  ← Trade execution script
geometric_engine.py             ← Core trendline algorithm
geometric_trendline_engine.py   ← Multi-timeframe trendline engine
notify_new_stocks.py            ← Telegram notifications for new signals
check_alerts.py                 ← Target/SL alert checker
update_trendline_prices_live.py ← Live price updater for trendline stocks
requirements.txt                ← Python dependencies
Stock List.csv                  ← NIFTY500 stock list

.github/workflows/
    run_screener.yml            ← Runs scanner every 15 min (market hours)
    monitor_trades.yml          ← Monitors trades every 30 min (market hours)
    angel_trade.yml             ← Executes trades (triggered by dashboard)
    deploy_backend.yml          ← Deploys angel_order_handler.py to EC2
```

---

## GitHub Actions Workflows

### `run_screener.yml` (Backend)
- **Trigger:** Every 15 min, Mon–Fri, 9:15 AM – 3:30 PM IST (cron UTC)
- **What it does:**
  1. Runs `update_feed.py` — scans 750+ NSE stocks for trendline signals
  2. Runs `update_trendline_prices_live.py` — updates live prices
  3. Pushes `trendline_screen.json` + `data.json` to `Stock-Yard-Public` using `PUBLIC_REPO_TOKEN`
  4. Saves state to backend repo

### `monitor_trades.yml` (Backend)
- **Trigger:** Every 30 min, Mon–Fri, 9:15 AM – 3:30 PM IST
- **What it does:**
  1. Runs `angel_monitor.py` — checks open trades for target/SL hits, syncs with Angel One
  2. Pushes `radar_trades.json` to `Stock-Yard-Public`
  3. Sends Telegram alerts when positions close

### `angel_trade.yml` (Backend)
- **Trigger:** `repository_dispatch` event type `place_trade` (called by dashboard)
- **What it does:**
  1. Runs `angel_trade.py` with trade parameters
  2. Places order on Angel One
  3. Pushes updated `radar_trades.json` to `Stock-Yard-Public`

### `deploy_backend.yml` (Backend)
- **Trigger:** Push to `main` when `angel_order_handler.py` changes
- **What it does:**
  1. SSHs into EC2 server
  2. Copies latest `angel_order_handler.py`
  3. Restarts `angel-api` systemd service

### `pages.yml` (Public)
- **Trigger:** Every push to `main` in `Stock-Yard-Public`
- **What it does:** Deploys the repo root to GitHub Pages

---

## EC2 API Server

**Host:** `http://32.194.58.75:5000`  
**File:** `angel_order_handler.py` (Flask app)  
**Service:** `angel-api` (systemd)

**Endpoints:**
| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | Health check |
| `/api/get-quote?symbol=INFY` | GET | Live LTP from Angel One (yfinance fallback) |
| `/api/place-order` | POST | Place limit order on Angel One |
| `/api/sync-trades` | GET | Fetch open positions from Angel One |

**Angel One session:** The EC2 app authenticates using TOTP on startup. Session lasts ~8 hours. If LTP fetch fails, it falls back to yfinance automatically.

---

## Secrets (Backend repo — `Stock-Yard`)

| Secret Name | Description |
|---|---|
| `ANGEL_API_KEY` | Angel One SmartAPI key |
| `ANGEL_CLIENT_ID` | Angel One client ID |
| `ANGEL_PASSWORD` | Angel One login password |
| `ANGEL_TOTP_SECRET` | TOTP secret for 2FA |
| `EC2_HOST` | EC2 server IP (`32.194.58.75`) |
| `EC2_USER` | EC2 SSH username (`ubuntu`) |
| `EC2_SSH_KEY` | EC2 SSH private key (full PEM content) |
| `PUBLIC_REPO_TOKEN` | GitHub PAT with `repo` write scope on `Stock-Yard-Public` |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token for alerts |
| `TELEGRAM_CHAT_ID` | Telegram chat/user ID for alerts |

---

## Dashboard Features (`index.html`)

### Tabs
1. **Volume** — Volume breakout stocks from `data.json`
2. **Trendline** — Trendline signal stocks from `trendline_screen.json`
3. **Radar** — Active trades from `radar_trades.json` + `radarOverrides` (localStorage)
4. **Performance** — Backtested performance data

### Radar Tab
- Loads `radar_trades.json` as source of truth
- User overrides (status changes, field edits) stored in `localStorage.radarOverrides`
- **Add Trade** modal — type 3+ chars to search NIFTY500, fill entry/qty/target/SL, select source broker
- **Filter** buttons — All / Open / Closed
- **Live LTP** — fetched from EC2 `/api/get-quote` in background, saved to overrides
- **Target Hit / Stop Loss** — marks trade closed, saves to overrides
- **Delete** — removes from radar, returns to Trendline list

### Trade Flow (Trendline → Radar)
```
1. Entry price approaches trendline trigger
   → Angel monitor detects → adds to radar_trades.json → Telegram alert with buttons
2. User clicks "✅ Confirm Trade" in Telegram
   → Opens dashboard with ?confirm=TICKER&price=...
   → Dashboard shows confirm dialog
   → On confirm: calls EC2 /api/place-order → order placed on Angel One
   → Workflow pushes updated radar_trades.json to Public repo
3. Trade appears in Radar tab
4. Monitor checks every 30 min for target/SL hits
```

### Add Trade to Radar (Manual)
For trades placed outside Angel One (Zerodha, Groww, etc.):
- Click **+ Add Trade** in Radar tab
- Search symbol (3+ chars autocomplete)
- Fill entry price, quantity, target, stop loss, source broker
- Saves to `localStorage.radarOverrides` with `_custom: true`
- Live LTP fetched from EC2, saved back to overrides

---

## Data Flow Diagram

```
Stock-Yard (Private Backend)
│
├── GitHub Actions (scheduled)
│   ├── run_screener.yml
│   │   ├── update_feed.py → trendline_screen.json, data.json
│   │   └── git push → Stock-Yard-Public/trendline_screen.json
│   │                   Stock-Yard-Public/data.json
│   │
│   └── monitor_trades.yml
│       ├── angel_monitor.py → radar_trades.json
│       └── git push → Stock-Yard-Public/radar_trades.json
│
├── EC2 Flask API (32.194.58.75:5000)
│   ├── /api/get-quote    → Angel One LTP (yfinance fallback)
│   ├── /api/place-order  → Angel One order placement
│   └── /api/sync-trades  → Angel One open positions
│
Stock-Yard-Public (Public Frontend)
│
├── index.html (GitHub Pages)
│   ├── Fetches: trendline_screen.json, data.json, radar_trades.json
│   ├── Calls:   EC2 API for live LTP
│   └── Sends:   repository_dispatch to Stock-Yard for trade execution
│
└── GitHub Pages → https://anuragsin17-sketch.github.io/Stock-Yard-Public/
```

---

## Local Development

### Public repo (UI changes)
```
cd "d:\Stock Yard"
# Edit index.html
git add index.html
git commit -m "feat: your change description"
git push origin main
# GitHub Pages deploys automatically in ~2 min
```

### Backend repo (Python/workflow changes)
```
cd "d:\Stock Yard Backend"
# Edit Python scripts or workflows
git add .
git commit -m "fix: your change description"
git push origin main
```

### Local testing
```
cd "d:\Stock Yard"
python -m http.server 8000
# Open http://localhost:8000
```

---

## Common Issues & Fixes

| Issue | Cause | Fix |
|---|---|---|
| Dashboard shows old version | GitHub Pages CDN cache | Ctrl+Shift+R (hard refresh) or open incognito |
| Radar shows old trades | `radarOverrides` in localStorage | Open F12 console → `localStorage.removeItem('radarOverrides')` |
| LTP not updating | EC2 API down or Angel One session expired | Check `http://32.194.58.75:5000/health` |
| Workflow queued but not running | No runner available | Workflows use `ubuntu-latest` (GitHub-hosted) — should auto-start |
| Trade not appearing in Radar | Order placed but monitor not run yet | Wait for next 30-min monitor cycle or run manually |
| Angel One session expired | TOTP session ~8h | Restart EC2 service: `sudo systemctl restart angel-api` |

---

## Making Changes — Checklist

### To update the UI (`index.html`)
- [ ] Edit locally in `d:\Stock Yard`
- [ ] Test with `python -m http.server 8000`
- [ ] `git push origin main` from `d:\Stock Yard`
- [ ] Wait ~2 min → check GitHub Pages

### To update the screener (Python)
- [ ] Edit in `d:\Stock Yard Backend`
- [ ] `git push origin main` from `d:\Stock Yard Backend`
- [ ] Trigger `Stock Yard Screener` workflow manually to test

### To update the EC2 API
- [ ] Edit `angel_order_handler.py` in `d:\Stock Yard Backend`
- [ ] `git push origin main`
- [ ] `Deploy Angel Order Handler to EC2` workflow runs automatically
