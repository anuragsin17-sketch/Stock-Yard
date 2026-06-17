#!/bin/bash
# ============================================================
# Setup EC2 cron for 5-minute fib price alert
# Run once on EC2: bash setup_fib_cron.sh
# ============================================================

REPO_DIR=$(pwd)
LOG_DIR="$REPO_DIR/logs"
mkdir -p "$LOG_DIR"

echo "Setting up fib_price_alert cron..."
echo "Repo: $REPO_DIR"

# Write cron entries to a temp file
CRON_FILE=$(mktemp)

# Keep existing cron jobs
crontab -l 2>/dev/null > "$CRON_FILE" || true

# Remove any old fib_price_alert entries
grep -v "fib_price_alert" "$CRON_FILE" > "${CRON_FILE}.clean" && mv "${CRON_FILE}.clean" "$CRON_FILE"

# Add 5-minute fib alert during market hours IST (Mon-Fri)
# IST 09:15-15:30 = UTC 03:45-10:00
# Run every 5 min from 03:40 UTC to 10:05 UTC

cat >> "$CRON_FILE" << 'CRON'
# Stock Yard — Fib Price Alert every 5 min (market hours)
*/5 4-9 * * 1-5 cd /home/ubuntu/Stock-Yard && python fib_price_alert.py >> logs/fib_alert.log 2>&1
0,5,10,15,20,25,30 10 * * 1-5 cd /home/ubuntu/Stock-Yard && python fib_price_alert.py >> logs/fib_alert.log 2>&1
40 3 * * 1-5 cd /home/ubuntu/Stock-Yard && python fib_price_alert.py >> logs/fib_alert.log 2>&1
# Stock Yard — Live price monitor every 1 min (market hours) — trendline + volume
* 4-9 * * 1-5 cd /home/ubuntu/Stock-Yard && python live_price_monitor.py >> logs/live_monitor.log 2>&1
0-30 10 * * 1-5 cd /home/ubuntu/Stock-Yard && python live_price_monitor.py >> logs/live_monitor.log 2>&1
45 3 * * 1-5 cd /home/ubuntu/Stock-Yard && python live_price_monitor.py >> logs/live_monitor.log 2>&1
# Rotate logs at midnight (keep last 1000 lines)
0 0 * * * tail -1000 /home/ubuntu/Stock-Yard/logs/live_monitor.log > /home/ubuntu/Stock-Yard/logs/live_monitor.log.tmp && mv /home/ubuntu/Stock-Yard/logs/live_monitor.log.tmp /home/ubuntu/Stock-Yard/logs/live_monitor.log
0 0 * * * tail -500 /home/ubuntu/Stock-Yard/logs/fib_alert.log > /home/ubuntu/Stock-Yard/logs/fib_alert.log.tmp && mv /home/ubuntu/Stock-Yard/logs/fib_alert.log.tmp /home/ubuntu/Stock-Yard/logs/fib_alert.log
CRON

# Install
crontab "$CRON_FILE"
rm "$CRON_FILE"

echo ""
echo "Cron installed. Current crontab:"
crontab -l
echo ""
echo "Verify env vars are set on EC2:"
echo "  export TELEGRAM_BOT_TOKEN=..."
echo "  export TELEGRAM_CHAT_ID=..."
echo ""
echo "Or add to /etc/environment for persistence."
echo ""
echo "Test run:"
echo "  python fib_price_alert.py"
