#!/usr/bin/env python3
"""
DynamoDB Helper — Stock Yard
Shared read/write functions used by:
  - angel_order_handler.py  (EC2 Flask API)
  - update_feed.py           (trendline screener)
  - angel_monitor.py         (trade monitor)
  - volume_gainer_monitor.py (volume screener)

Usage (migrate existing JSON → DynamoDB):
    python3 dynamodb_helper.py --migrate
"""

import os
import json
import boto3
import argparse
from decimal import Decimal
from datetime import datetime
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

AWS_REGION = os.environ.get('AWS_REGION', 'us-east-1')  # same region as EC2

# ── DynamoDB client (uses EC2 IAM role in production, env vars locally) ──────

def _db():
    return boto3.resource('dynamodb', region_name=AWS_REGION)

def _table(name):
    return _db().Table(name)


# ── Decimal conversion (DynamoDB requires Decimal, not float) ─────────────────

def _to_decimal(obj):
    """Recursively convert floats to Decimal for DynamoDB."""
    if isinstance(obj, float):
        return Decimal(str(round(obj, 6)))
    if isinstance(obj, dict):
        return {k: _to_decimal(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_decimal(i) for i in obj]
    return obj

def _from_decimal(obj):
    """Recursively convert Decimal back to float for JSON serialisation."""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _from_decimal(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_from_decimal(i) for i in obj]
    return obj


# ── StockSignals ──────────────────────────────────────────────────────────────

SIGNAL_TYPES = ('TRENDLINE', 'VOLUME', 'GOLDEN')

def write_signals(signal_type: str, stocks: list):
    """
    Replace all signals of a given type.
    signal_type: 'TRENDLINE' | 'VOLUME' | 'GOLDEN'
    stocks: list of dicts (one per stock)
    """
    assert signal_type in SIGNAL_TYPES, f"Unknown signal_type: {signal_type}"
    table = _table('StockSignals')

    # Delete old records for this signal type first
    existing = table.query(
        KeyConditionExpression=Key('signal_type').eq(signal_type),
        ProjectionExpression='signal_type, ticker'
    )
    with table.batch_writer() as batch:
        for item in existing.get('Items', []):
            batch.delete_item(Key={'signal_type': item['signal_type'], 'ticker': item['ticker']})

    # Write new records
    with table.batch_writer() as batch:
        for stock in stocks:
            ticker = (stock.get('ticker') or stock.get('symbol') or '').upper()
            if not ticker:
                continue
            item = _to_decimal(stock)
            item['signal_type'] = signal_type
            item['ticker'] = ticker
            item['updated_at'] = datetime.utcnow().isoformat()
            batch.put_item(Item=item)

    print(f"  ✅ DynamoDB: wrote {len(stocks)} {signal_type} signals")


def read_signals(signal_type: str) -> list:
    """Read all signals of a given type. Returns list of dicts."""
    table = _table('StockSignals')
    result = table.query(
        KeyConditionExpression=Key('signal_type').eq(signal_type)
    )
    return _from_decimal(result.get('Items', []))


# ── RadarTrades ───────────────────────────────────────────────────────────────

def _make_trade_id(ticker: str, triggered_at: str = None) -> str:
    ts = triggered_at or datetime.utcnow().isoformat()
    # Use only date+time compact form as part of the key
    compact = ts.replace(':', '').replace('-', '').replace('T', 'T')[:15]
    return f"{ticker.upper()}_{compact}"


def write_trade(trade: dict):
    """Insert or update a single trade."""
    table = _table('RadarTrades')
    ticker = trade.get('ticker', '').upper()
    trade_id = trade.get('trade_id') or _make_trade_id(ticker, trade.get('triggered_at'))
    status = trade.get('status', 'Open')

    item = _to_decimal(trade)
    item['ticker'] = ticker
    item['trade_id'] = trade_id
    item['status'] = status
    item['updated_at'] = datetime.utcnow().isoformat()

    table.put_item(Item=item)
    return trade_id


def write_all_trades(trades: list):
    """Replace all trades in DynamoDB from a list (mirrors radar_trades.json)."""
    table = _table('RadarTrades')

    # Clear existing
    for status in ('Open', 'Triggered', 'Closed'):
        existing = table.query(
            KeyConditionExpression=Key('status').eq(status),
            ProjectionExpression='#s, trade_id',
            ExpressionAttributeNames={'#s': 'status'}
        )
        with table.batch_writer() as batch:
            for item in existing.get('Items', []):
                batch.delete_item(Key={'status': item['status'], 'trade_id': item['trade_id']})

    # Write all
    with table.batch_writer() as batch:
        for trade in trades:
            ticker = (trade.get('ticker') or '').upper()
            if not ticker:
                continue
            trade_id = trade.get('trade_id') or _make_trade_id(ticker, trade.get('triggered_at'))
            status = trade.get('status', 'Open')
            item = _to_decimal(trade)
            item['ticker'] = ticker
            item['trade_id'] = trade_id
            item['status'] = status
            item['updated_at'] = datetime.utcnow().isoformat()
            batch.put_item(Item=item)

    print(f"  ✅ DynamoDB: wrote {len(trades)} trades")


def read_trades(status_filter: str = None) -> list:
    """
    Read trades from DynamoDB.
    status_filter: 'Open' | 'Triggered' | 'Closed' | None (returns all)
    """
    table = _table('RadarTrades')
    statuses = [status_filter] if status_filter else ['Open', 'Triggered', 'Closed']
    trades = []
    for status in statuses:
        result = table.query(
            KeyConditionExpression=Key('status').eq(status)
        )
        trades.extend(result.get('Items', []))
    return _from_decimal(trades)


def update_trade_status(ticker: str, trade_id: str, old_status: str, new_status: str, extra: dict = None):
    """Move a trade from one status partition to another (requires delete + re-insert)."""
    table = _table('RadarTrades')

    # Read existing
    resp = table.get_item(Key={'status': old_status, 'trade_id': trade_id})
    item = resp.get('Item')
    if not item:
        print(f"  ⚠️  Trade not found: {ticker}/{trade_id} status={old_status}")
        return

    # Delete old
    table.delete_item(Key={'status': old_status, 'trade_id': trade_id})

    # Re-insert with new status
    item['status'] = new_status
    item['updated_at'] = datetime.utcnow().isoformat()
    if extra:
        item.update(_to_decimal(extra))
    table.put_item(Item=item)
    print(f"  ✅ Trade {ticker} moved: {old_status} → {new_status}")


# ── LivePrices ────────────────────────────────────────────────────────────────

def write_price(ticker: str, ltp: float, source: str = 'angel'):
    """Update LTP for a single ticker."""
    table = _table('LivePrices')
    table.put_item(Item={
        'ticker':     ticker.upper(),
        'ltp':        Decimal(str(round(ltp, 2))),
        'source':     source,
        'updated_at': datetime.utcnow().isoformat(),
    })


def write_prices_bulk(prices: dict):
    """
    Write many prices at once.
    prices: { 'INFY': 1800.5, 'TCS': 3400.0, ... }
    """
    table = _table('LivePrices')
    now = datetime.utcnow().isoformat()
    with table.batch_writer() as batch:
        for ticker, ltp in prices.items():
            if ltp and ltp > 0:
                batch.put_item(Item={
                    'ticker':     ticker.upper(),
                    'ltp':        Decimal(str(round(float(ltp), 2))),
                    'updated_at': now,
                })
    print(f"  ✅ DynamoDB: wrote {len(prices)} live prices")


def read_prices(tickers: list = None) -> dict:
    """
    Read LTPs from DynamoDB.
    tickers: list of tickers to fetch (None = all)
    Returns: { 'INFY': 1800.5, ... }
    """
    table = _table('LivePrices')
    if tickers:
        # Batch get for specific tickers
        keys = [{'ticker': t.upper()} for t in tickers]
        resp = _db().batch_get_item(RequestItems={
            'LivePrices': {'Keys': keys}
        })
        items = resp.get('Responses', {}).get('LivePrices', [])
    else:
        resp = table.scan()
        items = resp.get('Items', [])

    return {item['ticker']: float(item['ltp']) for item in items if item.get('ltp')}


def read_price(ticker: str) -> float:
    """Read LTP for a single ticker. Returns 0 if not found."""
    table = _table('LivePrices')
    resp = table.get_item(Key={'ticker': ticker.upper()})
    item = resp.get('Item')
    return float(item['ltp']) if item and item.get('ltp') else 0.0


# ── Migration: JSON files → DynamoDB ─────────────────────────────────────────

def migrate_from_json():
    """One-time migration: load existing JSON files into DynamoDB."""
    print("\nMigrating JSON files → DynamoDB...\n")

    # trendline_screen.json → TRENDLINE signals
    for fname, stype in [('trendline_screen.json', 'TRENDLINE'), ('data.json', 'VOLUME')]:
        if os.path.exists(fname):
            with open(fname) as f:
                data = json.load(f)
            if isinstance(data, dict):
                # data.json may have nested keys
                stocks = (data.get('volume_gainer_stocks') or
                          data.get('stocks') or
                          data.get('volume_breakout_stocks') or [])
            else:
                stocks = data
            if stocks:
                write_signals(stype, stocks)
                print(f"  Migrated {len(stocks)} stocks from {fname}")
        else:
            print(f"  Skipping {fname} (not found)")

    # radar_trades.json → RadarTrades
    if os.path.exists('radar_trades.json'):
        with open('radar_trades.json') as f:
            trades = json.load(f)
        if isinstance(trades, list) and trades:
            write_all_trades(trades)
            print(f"  Migrated {len(trades)} trades from radar_trades.json")
    else:
        print("  Skipping radar_trades.json (not found)")

    print("\n✅ Migration complete\n")


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--migrate', action='store_true', help='Migrate JSON files to DynamoDB')
    parser.add_argument('--test',    action='store_true', help='Run a quick read test')
    args = parser.parse_args()

    if args.migrate:
        migrate_from_json()
    elif args.test:
        print("Testing DynamoDB read...")
        signals = read_signals('TRENDLINE')
        print(f"  TRENDLINE signals: {len(signals)}")
        trades = read_trades()
        print(f"  Trades: {len(trades)}")
        prices = read_prices()
        print(f"  Live prices cached: {len(prices)}")
    else:
        parser.print_help()
