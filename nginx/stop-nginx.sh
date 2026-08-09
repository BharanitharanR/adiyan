#!/bin/bash

# Stop Adiyan Nginx Reverse Proxy

PID_FILE="/tmp/adiyan_nginx.pid"

echo "🛑 Stopping Adiyan Nginx Reverse Proxy"

if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "   Stopping process (PID: $PID)"
        kill -QUIT "$PID"
        sleep 2

        if kill -0 "$PID" 2>/dev/null; then
            echo "   Force killing..."
            kill -9 "$PID"
        fi

        echo "✅ Nginx stopped"
    else
        echo "⚠️  Process not running (PID: $PID)"
        rm -f "$PID_FILE"
    fi
else
    echo "ℹ️  No PID file found. Nginx might not be running."

    # Try to kill all nginx processes
    PIDS=$(pgrep -f "nginx.*adiyan" | grep -v grep)
    if [ -n "$PIDS" ]; then
        echo "   Found running processes: $PIDS"
        echo "$PIDS" | xargs kill -9
        echo "✅ Killed nginx processes"
    fi
fi
