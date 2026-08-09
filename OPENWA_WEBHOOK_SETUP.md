# OpenWA Webhook Integration - Setup Guide

**Status**: ✅ **Complete & Ready to Use**

## What Was Built

The webhook has been fully integrated into the Adiyan orchestrator:

1. **OpenWA Receiver Service** (`services/openwa_receiver.py`)
   - Converts OpenWA webhook format to Adiyan message format
   - Feeds messages directly into the orchestrator pipeline
   - No RabbitMQ needed for OpenWA messages

2. **Webhook Endpoint** (`/webhook/openwa`)
   - Listens for OpenWA webhook POST requests
   - Validates and processes messages
   - Returns acknowledgment to OpenWA

3. **Setup Script** (`setup_openwa_webhook.py`)
   - Interactive configuration tool
   - Automatically registers webhook with OpenWA
   - Tests connectivity before registration

---

## Quick Start (5 Minutes)

### Step 1: Start Services

**Terminal 1 - Start Adiyan:**
```bash
cd /Users/bharani/Desktop/aiAgentCompaction/Adiyan
python main.py
```

Wait for logs to show:
```
✅ OpenWA Receiver initialized
✅ All 7 agents initialized
Ready for messages!
```

**Terminal 2 - Start OpenWA:**
```bash
cd /Users/bharani/Desktop/aiAgentCompaction/Adiyan/penwa
npm start
```

Wait for logs to show:
```
✅ NestApplication successfully started
🌐 OpenWA API listening on port 2785
```

### Step 2: Register Webhook

**Terminal 3 - Run setup script:**
```bash
cd /Users/bharani/Desktop/aiAgentCompaction/Adiyan
python setup_openwa_webhook.py
```

The script will ask for:
- **OpenWA URL**: `http://localhost:2785` (press Enter)
- **API Key**: Get from http://localhost:2785 → Settings → API Keys
- **Session ID**: `adiyan-coaching` (or whatever you created)
- **Adiyan Host**: `localhost` (press Enter)
- **Adiyan Port**: `5001` (press Enter)
- **Webhook Secret**: `adiyan-secret-key` (or choose your own)

Confirm with `y` and it will register! ✅

### Step 3: Test End-to-End Flow

1. **Go to OpenWA dashboard**: http://localhost:2785
2. **Create a WhatsApp session** (if not done already):
   - Click "Create Session"
   - Name it: `adiyan-coaching`
   - Click Create
3. **Scan QR code** with WhatsApp mobile app (Settings → Linked Devices)
4. **Send test message** from your phone to the coaching number

**Expected flow:**
```
WhatsApp User sends message
    ↓ (via WhatsApp Web)
OpenWA receives on port 2785
    ↓ (via webhook POST)
Adiyan receives on http://localhost:5001/webhook/openwa
    ↓ (through 7-stage pipeline)
Adiyan response sent to OpenWA API
    ↓
OpenWA sends to WhatsApp
    ↓
WhatsApp User receives coaching response ✅
```

---

## File Structure

```
Adiyan/
├── services/
│   └── openwa_receiver.py          # NEW: Webhook receiver & adapter
├── ui/
│   └── control_panel_api.py        # UPDATED: Added webhook endpoint
├── main.py                          # UPDATED: Initialize OpenWA receiver
├── setup_openwa_webhook.py         # NEW: Interactive setup script
├── OPENWA_WEBHOOK_SETUP.md         # This file
└── penwa/                           # OpenWA installation
    ├── dist/                        # Built source
    ├── .env                         # Config
    └── npm start                    # Run it
```

---

## Manual Setup (If Script Doesn't Work)

### Create Session in OpenWA

```bash
API_KEY="<your-api-key-from-dashboard>"
SESSION_ID="adiyan-coaching"

curl -X POST http://localhost:2785/api/sessions \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "'$SESSION_ID'"}'

# Response:
# {
#   "sessionId": "adiyan-coaching",
#   "status": "created"
# }
```

### Get QR Code

```bash
curl -H "X-API-Key: $API_KEY" \
  http://localhost:2785/api/sessions/$SESSION_ID/qr

# Scan the QR code with WhatsApp mobile
```

### Register Webhook

```bash
API_KEY="<your-api-key>"
SESSION_ID="adiyan-coaching"

curl -X POST \
  "http://localhost:2785/api/sessions/$SESSION_ID/webhooks" \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "http://localhost:5001/webhook/openwa",
    "events": ["message.received"],
    "secret": "adiyan-secret-key",
    "retryCount": 3
  }'

# Response should include webhook ID:
# {
#   "id": "webhook-id-here",
#   "url": "http://localhost:5001/webhook/openwa",
#   "events": ["message.received"]
# }
```

---

## How It Works

### Message Flow

1. **OpenWA receives WhatsApp message**
   ```json
   {
     "event": "message.received",
     "data": {
       "sessionId": "adiyan-coaching",
       "senderName": "Sripriya",
       "body": "How do I improve my productivity?",
       ...
     }
   }
   ```

2. **OpenWA POSTs to Adiyan webhook**
   ```
   POST http://localhost:5001/webhook/openwa
   Content-Type: application/json
   Body: {...webhook data...}
   ```

3. **Adiyan webhook endpoint receives**
   ```python
   @app.route('/webhook/openwa', methods=['POST'])
   async def openwa_webhook():
       # Gets webhook data from request
       receiver = app.config.get('OPENWA_RECEIVER')
       result = await receiver.process_webhook(webhook_data)
       return jsonify(result), 202
   ```

4. **OpenWA Adapter converts format**
   ```python
   OpenWAAdapter.webhook_to_message(webhook_data)
   # Converts OpenWA format → AgentState format
   ```

5. **Sends to orchestrator**
   ```python
   await self.orchestrator_callback(message)
   # Processes through: Parser → Validator → Router → LLM → Synthesizer → Storage → Publisher
   ```

6. **Publisher Agent sends response**
   ```python
   # PublisherAgent calls OpenWA API to send message back:
   POST http://localhost:2785/api/sessions/{id}/message/send
   {
     "chatId": "919080089081@c.us",
     "message": "Here are 3 tips to improve your productivity..."
   }
   ```

7. **OpenWA sends to WhatsApp** ✅

---

## Configuration

### Environment Variables

Add to `.env` (optional):
```bash
# OpenWA
OPENWA_URL=http://localhost:2785
OPENWA_WEBHOOK_SECRET=adiyan-secret-key

# Adiyan
ADIYAN_WEBHOOK_URL=http://localhost:5001/webhook/openwa
```

### Webhook Events

Register for any of these events:
```json
{
  "events": [
    "message.received",      // Incoming message
    "message.sent",          // Outgoing message delivered
    "message.ack",           // Message acknowledged
    "message.reaction",      // User added emoji reaction
    "session.status",        // Session status changed
    "session.authenticated", // Successfully authenticated
    "session.disconnected",  // Connection lost
    "presence.update"        // User online/offline status
  ]
}
```

---

## Troubleshooting

### Issue: Webhook not receiving messages

**Solution 1: Check Adiyan is running**
```bash
curl http://localhost:5001/api/health
# Should return: {"status":"ok"}
```

**Solution 2: Check webhook registered**
```bash
curl -H "X-API-Key: $API_KEY" \
  http://localhost:2785/api/sessions/adiyan-coaching/webhooks

# Should list registered webhooks
```

**Solution 3: Check OpenWA logs**
```bash
# Look for webhook delivery attempts in OpenWA logs
tail -f penwa/logs/openwa.log
```

**Solution 4: Test webhook manually**
```bash
curl -X POST http://localhost:5001/webhook/openwa \
  -H "Content-Type: application/json" \
  -d '{
    "event": "message.received",
    "data": {
      "sessionId": "adiyan-coaching",
      "senderName": "Test User",
      "body": "Test message"
    }
  }'

# Should return: {"status":"processed"}
```

### Issue: Messages not reaching WhatsApp

**Check logs:**
```bash
tail -f ~/.Adiyan/orchestrator.log | grep "Publisher\|OpenWA\|response"
```

**Check PublisherAgent is configured for OpenWA:**
```python
# In config/control_plane.py, should have:
USE_OPENWA=true
OPENWA_API_KEY=<your-key>
```

---

## Testing Commands

### Send Test Message via API

```bash
curl -X POST http://localhost:2785/api/sessions/adiyan-coaching/message/send \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "chatId": "919080089081@c.us",
    "message": "Test from API"
  }'
```

### Get Recent Messages

```bash
curl -H "X-API-Key: $API_KEY" \
  http://localhost:2785/api/sessions/adiyan-coaching/chats

# Shows all chats and messages
```

### Check Webhook Deliveries

```bash
curl -H "X-API-Key: $API_KEY" \
  http://localhost:2785/api/sessions/adiyan-coaching/webhooks/webhook-id/logs

# Shows webhook delivery history
```

---

## Next Steps

1. ✅ Start both services
2. ✅ Run setup script to register webhook
3. ✅ Send test message from WhatsApp
4. ✅ Verify message flows through pipeline
5. ✅ Check response is sent back

---

## Support

**Check logs in real-time:**
```bash
# Adiyan logs
tail -f ~/.Adiyan/orchestrator.log

# OpenWA logs (in penwa folder)
tail -f logs/openwa.log
```

**OpenWA Dashboard:**
- http://localhost:2785
- Check Sessions → Messages tab

**Adiyan Dashboard:**
- http://localhost:5001
- Check Logs tab

---

**Status**: Ready to go! 🚀

Questions? Check the logs or revisit this guide.
