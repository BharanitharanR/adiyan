#!/bin/bash

# Start Adiyan Nginx Reverse Proxy
# This proxies webhooks from OpenWA to Adiyan

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
CONFIG_FILE="$SCRIPT_DIR/adiyan.conf"
PID_FILE="/tmp/adiyan_nginx.pid"

echo "🚀 Starting Adiyan Nginx Reverse Proxy"
echo "   Config: $CONFIG_FILE"
echo "   Listen: http://localhost:8080"
echo "   Upstream: http://localhost:5001"
echo ""

# Check if already running
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "⚠️  Nginx already running (PID: $OLD_PID)"
        echo "   Stop it first: ./stop-nginx.sh"
        exit 1
    fi
fi

# Start nginx
nginx -c "$CONFIG_FILE" -p /tmp/adiyan_nginx_

if [ $? -eq 0 ]; then
    echo "✅ Nginx started successfully"
    echo ""
    echo "📋 Configuration:"
    echo "   Webhook URL: http://localhost:8080/webhook/openwa"
    echo "   Health check: http://localhost:8080/health"
    echo ""
    echo "📝 Register this webhook in OpenWA:"
    echo "   URL: http://localhost:8080/webhook/openwa"
    echo "   Events: message.received"
    echo ""
    echo "Logs:"
    echo "   Access: tail -f /tmp/adiyan_nginx_access.log"
    echo "   Error: tail -f /tmp/adiyan_nginx_error.log"
else
    echo "❌ Failed to start nginx"
    exit 1
fi
