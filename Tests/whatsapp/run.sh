#!/bin/bash

# Adiyan WhatsApp POC Test - Startup Script

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

echo "╔════════════════════════════════════════════════════════════════╗"
echo "║                  Adiyan WhatsApp POC Test                      ║"
echo "║                   3-Step Validation                            ║"
echo "╚════════════════════════════════════════════════════════════════╝"
echo ""

# Check if Node.js is installed
if ! command -v node &> /dev/null; then
    echo "❌ Node.js is not installed"
    echo "   Install it with: brew install node"
    exit 1
fi

echo "✅ Node.js version: $(node --version)"
echo ""

# Check if npm dependencies are installed
if [ ! -d "$SCRIPT_DIR/node_modules" ]; then
    echo "📦 Installing dependencies..."
    cd "$SCRIPT_DIR"
    npm install
    echo "✅ Dependencies installed"
    echo ""
fi

# Show what's about to happen
echo "🚀 Starting POC Test..."
echo ""
echo "Steps:"
echo "  1️⃣  Node.js will initialize WhatsApp connection"
echo "  2️⃣  You'll see a QR code to scan with your phone"
echo "  3️⃣  Scan with WhatsApp (Settings → Linked Devices)"
echo "  4️⃣  Send a test message from your phone"
echo "  5️⃣  Bot will reply with captured message details"
echo ""
echo "Logs: tail -f ~/.Adiyan/poc_test.log"
echo "Reset session: npm run reset"
echo ""
echo "─────────────────────────────────────────────────────────────────"
echo ""

# Run the POC test
cd "$SCRIPT_DIR"
node poc_test.js
