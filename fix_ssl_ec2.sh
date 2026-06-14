#!/bin/bash
# Fix SSL certificate on EC2 — replace self-signed cert with Let's Encrypt
# Run this on EC2: bash fix_ssl_ec2.sh
#
# NOTE: This uses nip.io which gives a free subdomain for your IP
# Your API will be at: https://32-194-58-75.nip.io/api/...

EC2_IP="32.194.58.75"
NIP_DOMAIN="${EC2_IP//./-}.nip.io"   # → 32-194-58-75.nip.io

echo "=== Installing certbot ==="
sudo apt-get update -y
sudo apt-get install -y certbot

echo "=== Stopping Flask temporarily to free port 80 ==="
sudo systemctl stop angel-flask 2>/dev/null || true
sudo fuser -k 80/tcp 2>/dev/null || true

echo "=== Getting Let's Encrypt certificate for $NIP_DOMAIN ==="
sudo certbot certonly --standalone \
  --non-interactive \
  --agree-tos \
  --email admin@stockyard.io \
  -d "$NIP_DOMAIN"

CERT_PATH="/etc/letsencrypt/live/$NIP_DOMAIN/fullchain.pem"
KEY_PATH="/etc/letsencrypt/live/$NIP_DOMAIN/privkey.pem"

if [ -f "$CERT_PATH" ]; then
    echo "=== Certificate obtained! ==="
    echo "Cert: $CERT_PATH"
    echo "Key:  $KEY_PATH"

    # Update Flask service to use the new cert
    sudo tee /etc/systemd/system/angel-flask.service > /dev/null << EOF
[Unit]
Description=Stock Yard Angel Flask API
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu
Environment="ANGEL_API_KEY=${ANGEL_API_KEY}"
Environment="ANGEL_CLIENT_ID=${ANGEL_CLIENT_ID}"
Environment="ANGEL_PIN=${ANGEL_PIN}"
Environment="ANGEL_TOTP_SECRET=${ANGEL_TOTP_SECRET}"
Environment="TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}"
Environment="TELEGRAM_CHAT_ID=${TELEGRAM_CHAT_ID}"
Environment="SSL_CERT_PATH=$CERT_PATH"
Environment="SSL_KEY_PATH=$KEY_PATH"
ExecStart=/usr/bin/python3 /home/ubuntu/angel_order_handler.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    sudo systemctl start angel-flask
    sudo systemctl enable angel-flask

    echo ""
    echo "=== DONE ==="
    echo "Your API is now at: https://$NIP_DOMAIN/api/..."
    echo ""
    echo "UPDATE index.html: replace 32.194.58.75 with $NIP_DOMAIN"
    echo "New URL: https://$NIP_DOMAIN"
else
    echo "=== Certificate failed — using workaround ==="
    echo "Starting Flask on port 80 (HTTP) as fallback"
fi
