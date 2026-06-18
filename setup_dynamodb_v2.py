#!/usr/bin/env python3
"""
DynamoDB v2 Setup — Stock Yard Dashboard Tables
Creates 3 tables used by the dashboard:
  - StockSignals   → trendline + volume screener results
  - RadarTrades    → open/closed trades
  - LivePrices     → latest LTP per ticker

Run once on EC2 or locally (needs AWS credentials):
    python3 setup_dynamodb_v2.py

Uses PAY_PER_REQUEST billing (free tier: 25 RCU + 25 WCU shared across all tables).
"""

import boto3
import os
from botocore.exceptions import ClientError

AWS_REGION = os.environ.get('AWS_REGION', 'us-east-1')  # same region as EC2


def get_dynamodb():
    """Get DynamoDB resource using env vars (works on EC2 IAM role or local credentials)."""
    return boto3.resource('dynamodb', region_name=AWS_REGION)


def create_table_safe(dynamodb, table_def):
    """Create table, skip if already exists."""
    name = table_def['TableName']
    try:
        table = dynamodb.create_table(**table_def)
        table.wait_until_exists()
        print(f"  ✅ Created: {name}")
        return table
    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceInUseException':
            print(f"  ✓  Already exists: {name}")
            return dynamodb.Table(name)
        raise


def setup():
    dynamodb = get_dynamodb()
    print(f"\n{'='*55}")
    print(f"  Stock Yard — DynamoDB Setup  (region: {AWS_REGION})")
    print(f"{'='*55}\n")

    # ── Table 1: StockSignals ──────────────────────────────────
    # Stores trendline + volume screener results.
    # PK: signal_type (TRENDLINE | VOLUME | GOLDEN)
    # SK: ticker
    create_table_safe(dynamodb, {
        'TableName': 'StockSignals',
        'KeySchema': [
            {'AttributeName': 'signal_type', 'KeyType': 'HASH'},
            {'AttributeName': 'ticker',      'KeyType': 'RANGE'},
        ],
        'AttributeDefinitions': [
            {'AttributeName': 'signal_type', 'AttributeType': 'S'},
            {'AttributeName': 'ticker',      'AttributeType': 'S'},
        ],
        'BillingMode': 'PAY_PER_REQUEST',
        'Tags': [{'Key': 'app', 'Value': 'stock-yard'}],
    })

    # ── Table 2: RadarTrades ───────────────────────────────────
    # Stores all trades (open + closed).
    # PK: status (Open | Closed | Triggered)
    # SK: trade_id  (ticker + timestamp, e.g. "INFY_20260618T093000")
    create_table_safe(dynamodb, {
        'TableName': 'RadarTrades',
        'KeySchema': [
            {'AttributeName': 'status',   'KeyType': 'HASH'},
            {'AttributeName': 'trade_id', 'KeyType': 'RANGE'},
        ],
        'AttributeDefinitions': [
            {'AttributeName': 'status',   'AttributeType': 'S'},
            {'AttributeName': 'trade_id', 'AttributeType': 'S'},
            {'AttributeName': 'ticker',   'AttributeType': 'S'},
        ],
        'GlobalSecondaryIndexes': [{
            'IndexName': 'TickerIndex',
            'KeySchema': [
                {'AttributeName': 'ticker',   'KeyType': 'HASH'},
                {'AttributeName': 'trade_id', 'KeyType': 'RANGE'},
            ],
            'Projection': {'ProjectionType': 'ALL'},
        }],
        'BillingMode': 'PAY_PER_REQUEST',
        'Tags': [{'Key': 'app', 'Value': 'stock-yard'}],
    })

    # ── Table 3: LivePrices ────────────────────────────────────
    # Stores latest LTP per ticker. Single row per ticker.
    # PK: ticker
    create_table_safe(dynamodb, {
        'TableName': 'LivePrices',
        'KeySchema': [
            {'AttributeName': 'ticker', 'KeyType': 'HASH'},
        ],
        'AttributeDefinitions': [
            {'AttributeName': 'ticker', 'AttributeType': 'S'},
        ],
        'BillingMode': 'PAY_PER_REQUEST',
        'Tags': [{'Key': 'app', 'Value': 'stock-yard'}],
    })

    print(f"\n{'='*55}")
    print(f"  ✅ All tables ready")
    print(f"{'='*55}")
    print(f"""
  Tables created in region: {AWS_REGION}
  ┌─────────────────┬──────────────────────────────┐
  │ StockSignals    │ trendline + volume signals    │
  │ RadarTrades     │ open + closed trades          │
  │ LivePrices      │ latest LTP per ticker         │
  └─────────────────┴──────────────────────────────┘

  Billing: PAY_PER_REQUEST (free tier: 25 RCU + 25 WCU)
  Cost: $0 under free tier for this app's usage.

  Next steps:
    1. Attach IAM role to EC2 with DynamoDB access
    2. Deploy angel_order_handler.py (has /api/signals, /api/radar)
    3. Run: python3 dynamodb_helper.py --migrate
       to load existing JSON files into DynamoDB
""")


if __name__ == '__main__':
    setup()
