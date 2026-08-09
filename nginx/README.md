# Adiyan Nginx Reverse Proxy

Nginx reverse proxy to expose Adiyan webhook endpoint at `http://localhost:8080` for OpenWA integration.

## Why Nginx?

OpenWA blocks localhost webhooks for security. Nginx acts as an intermediary:

```
OpenWA → http://localhost:8080/webhook/openwa (nginx)
                    ↓
         http://localhost:5001/webhook/openwa (Adiyan)
```

## Quick Start

### 1. Start Adiyan & OpenWA

```bash
# Terminal 1: Adiyan
cd /Users/bharani/Desktop/aiAgentCompaction/Adiyan
python main.py

# Terminal 2: OpenWA
cd penwa
npm start
```

### 2. Start Nginx

```bash
# Terminal 3: Nginx
cd /Users/bharani/Desktop/aiAgentCompaction/Adiyan/nginx
./start-nginx.sh
```

Output:
```
✅ Nginx started successfully

📋 Configuration:
   Webhook URL: http://localhost:8080/webhook/openwa
   Health check: http://localhost:8080/health
```

### 3. Register Webhook in OpenWA

Go to **http://localhost:2785**
- Click **Webhooks**
- Click **"Add Webhook"**
- Fill in:
  - **URL**: `http://localhost:8080/webhook/openwa`
  - **Events**: `message.received`
  - **Secret**: `adiyan-secret-key`
- Click **Save** ✅

### 4. Send Test Message

Send WhatsApp message from your phone → Message flows through Adiyan! ✅

---

## Logs

```bash
# Access log (requests)
tail -f /tmp/adiyan_nginx_access.log

# Error log (issues)
tail -f /tmp/adiyan_nginx_error.log
```

---

## Commands

### Start
```bash
./start-nginx.sh
```

### Stop
```bash
./stop-nginx.sh
```

### Check Status
```bash
curl http://localhost:8080/health
# Returns: {"status":"ok"}
```

### Test Webhook
```bash
curl -X POST http://localhost:8080/webhook/openwa \
  -H "Content-Type: application/json" \
  -d '{"event":"test"}'
```

---

## Configuration

Edit `adiyan.conf` to customize:

```nginx
# Change port (default 8080)
listen 9000;

# Add SSL/TLS
ssl_certificate /path/to/cert.pem;
ssl_certificate_key /path/to/key.pem;

# Change upstream server
server localhost:5001;
```

Then restart:
```bash
./stop-nginx.sh
./start-nginx.sh
```

---

## Troubleshooting

### Port already in use
```bash
lsof -ti:8080 | xargs kill -9
./start-nginx.sh
```

### Nginx won't start
```bash
# Check config syntax
nginx -t -c "$(pwd)/adiyan.conf"

# Check logs
tail -f /tmp/adiyan_nginx_error.log
```

### Webhook still blocked
If OpenWA still blocks localhost even with nginx:
- Check OpenWA logs
- Try a different port (9000, 9001, etc.)
- Use actual hostname instead of localhost

---

## Architecture

```
┌─────────────────────┐
│   WhatsApp User     │
└──────────┬──────────┘
           │
           ↓
┌─────────────────────┐
│   OpenWA (2785)     │  Receives WhatsApp messages
└──────────┬──────────┘
           │
           ↓ (HTTP POST webhook)
┌─────────────────────┐
│  Nginx (8080)       │  Reverse proxy
└──────────┬──────────┘
           │
           ↓ (Forwards to)
┌─────────────────────┐
│  Adiyan (5001)      │  Processes through 7 agents
└──────────┬──────────┘
           │
           ↓ (Response via OpenWA API)
┌─────────────────────┐
│   OpenWA (2785)     │  Sends back to WhatsApp
└──────────┬──────────┘
           │
           ↓
┌─────────────────────┐
│   WhatsApp User     │  Receives coaching response ✅
└─────────────────────┘
```

---

**Status**: Ready to go! 🚀
