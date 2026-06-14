import json

with open('backtest_1year_results.json') as f:
    data = json.load(f)

trades = data['trades']
critical = [t for t in trades if t['signal_status'] == 'CRITICAL_TOUCH']

print(f'Total CRITICAL trades: {len(critical)}')
print()

# Outcome breakdown
for outcome in ['TARGET_HIT', 'STOP_LOSS', 'TIMEOUT', 'OPEN']:
    bucket = [t for t in critical if t['outcome'] == outcome]
    if bucket:
        avg_hold = sum(t['holding_days'] for t in bucket) / len(bucket)
        print(f'{outcome:<15}: {len(bucket):2d} trades | avg hold: {avg_hold:.0f} days')

print()

# SL trades - how quickly did they stop out?
sl_trades = [t for t in critical if t['outcome'] == 'STOP_LOSS']
print('STOP LOSS detail (sorted by hold days):')
for t in sorted(sl_trades, key=lambda x: x['holding_days']):
    sym = t['symbol']
    print(f'  {sym:<12} entry: {t["entry_date"]}  held: {t["holding_days"]:3d}d  pnl: {t["pnl_pct"]:+.1f}%  fib: {t["fib_score"]}')

print()

# Quick SL = stopped out within 30 days = monthly candle closed below trendline
quick_sl = [t for t in sl_trades if t['holding_days'] <= 30]
slow_sl  = [t for t in sl_trades if t['holding_days'] > 30]
print(f'Quick SL (<=30 days = monthly closed below): {len(quick_sl)} / {len(sl_trades)}')
print(f'Slow  SL (>30 days = held then broke down):  {len(slow_sl)} / {len(sl_trades)}')
print()
print(f'So monthly close held (did NOT close below trendline immediately): {len(critical) - len(quick_sl)} / {len(critical)} = {round((len(critical)-len(quick_sl))/len(critical)*100,1)}%')
