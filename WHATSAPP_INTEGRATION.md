# WhatsApp Integration with Adiyan

The WhatsApp bot is now fully integrated into Adiyan and controlled from the dashboard.

## Architecture

```
WhatsApp User
    │
    ▼
WhatsApp Bridge (services/whatsapp_bridge.py)
    │
    ├─▶ RabbitMQ (Publish incoming messages)
    │    │
    │    ▼
    │ Orchestrator (7-stage pipeline)
    │    │
    │    ▼
    │ RabbitMQ (Publish responses)
    │    │
    └─▶ Listen for replies
         │
         ▼
    Send to WhatsApp User
```

## Dashboard Integration

### QR Code Display
- **Location**: Top-left card in dashboard
- **Purpose**: Scan to connect WhatsApp
- **Behavior**:
  - Shows "Initializing..." while starting
  - Shows QR code when ready to scan
  - Shows "✅ Connected" once logged in
  - Auto-refreshes every 3 seconds

### Connection Status
- **Indicator**: Shows connection state
- **Auto-refresh**: Every 3 seconds
- **States**:
  - ⏳ Initializing WhatsApp...
  - 📱 Scan QR code to connect
  - ✅ WhatsApp Connected!
  - ❌ Connection error

## How to Use

### 1. Start Adiyan with WhatsApp

```bash
cd /Users/bharani/Desktop/aiAgentCompaction/Adiyan
python main.py
```

You'll see:
```
✅ Adiyan service initialized
✅ All 7 agents initialized
✅ RabbitMQ connected
🌐 Control Panel UI: http://localhost:5001
📱 WhatsApp Bridge started
✅ WhatsApp client initialized
```

### 2. Open Dashboard

Visit: **http://localhost:5001**

You should see:
- WhatsApp card with QR code
- "Scan QR code to connect" message
- Live QR code updating every 3 seconds

### 3. Scan QR Code

1. Open WhatsApp on your phone
2. Go to **Settings → Linked Devices**
3. Click **Link a Device**
4. Scan the QR code in the dashboard
5. Wait for connection (10-30 seconds)

### 4. Send Messages

Once connected, send messages from WhatsApp:

```
User: "Register me in your coaching program"
Adiyan: "✅ Registered: Your Name"

User: "How can I improve my productivity?"
Adiyan: [Full coaching response...]

User: "Unregister me"
Adiyan: "✅ Unregistered: Your Name"
```

## Message Flow

### 1. Incoming Message
```
WhatsApp → WhatsAppBridge
           │
           ▼
        RabbitMQ
        (messages.incoming)
           │
           ▼
        Orchestrator
        (7 stages)
```

### 2. Pipeline Processing
```
ParserAgent ─▶ ValidatorAgent ─▶ RouterAgent
    │              │               │
    ▼              ▼               ▼
LLMAgent ─▶ SynthesizerAgent ─▶ StorageAgent
                                   │
                                   ▼
                            PublisherAgent
                                   │
                                   ▼
                            RabbitMQ
                        (events.response_sent)
```

### 3. Response Delivery
```
RabbitMQ (events.response_sent)
    │
    ▼
WhatsAppBridge listens
    │
    ▼
Extract reply
    │
    ▼
Send to WhatsApp
```

## API Endpoints

### Get WhatsApp Status
```bash
curl http://localhost:5001/api/whatsapp/status
```

Response:
```json
{
  "connected": false,
  "qr_available": true,
  "qr_text": "00020106360014..."
}
```

### Get QR Code
```bash
curl http://localhost:5001/api/whatsapp/qr
```

Response:
```json
{
  "qr_text": "00020106360014...",
  "connected": false
}
```

## Features

✅ **Real-time QR Display** - Live updates in dashboard
✅ **Connection Status** - Visual indicator of connection state
✅ **Registration Tracking** - Whitelist auto-updated
✅ **Message History** - All messages stored in interaction_history.jsonl
✅ **Model Selection** - Change LLM model and test immediately
✅ **Error Handling** - Graceful failures with fallback

## Troubleshooting

### QR Code Not Showing
- Check RabbitMQ is running: `rabbitmq-server`
- Check Ollama is running: `ollama serve`
- Restart Adiyan: `python main.py`

### WhatsApp Connection Fails
- Try scanning again (QR refreshes every 3 seconds)
- Check WhatsApp version is up to date
- Ensure WhatsApp account is not logged in elsewhere

### Messages Not Received
- Check dashboard logs for errors
- Verify RabbitMQ is running
- Check ~/.Adiyan/interaction_history.jsonl for stored messages

### QR Expires
- Dashboard auto-refreshes every 3 seconds
- QR codes typically expire after 30-60 seconds
- Just re-scan the new QR that appears

## Configuration

Modify WhatsApp bridge behavior in `services/whatsapp_bridge.py`:

```python
# Auth storage location
self.auth_path = Path.home() / '.Adiyan' / 'whatsapp_auth'

# RabbitMQ URL
rabbitmq_url = 'amqp://guest:guest@localhost/'

# Reply queue name
queue='whatsapp-replies'
```

## Architecture Details

### WhatsAppBridge Class
- **setup_rabbitmq()**: Connect to message queue
- **setup_whatsapp()**: Initialize WhatsApp client
- **publish_to_orchestrator()**: Send message to pipeline
- **listen_for_replies()**: Receive responses from orchestrator
- **update_qr()**: Update QR code for dashboard
- **set_connected()**: Update connection status

### Integration Points
1. **Incoming**: WhatsApp → RabbitMQ (messages.incoming)
2. **Outgoing**: RabbitMQ (events.response_sent) → WhatsApp
3. **Status**: Dashboard polls `/api/whatsapp/qr` every 3 seconds
4. **Control**: All configuration via dashboard

## Production Deployment

For production use:

1. **Docker**: Containerize WhatsAppBridge separately
2. **Kubernetes**: Scale orchestrator independently
3. **Authentication**: Use OAuth for WhatsApp
4. **Monitoring**: Track message latency and success rates
5. **Failover**: Implement retry logic for failed sends

## Performance

- **Message Latency**: 0.1-0.2 seconds (RabbitMQ)
- **LLM Processing**: 10-60 seconds (depends on model)
- **Total**: ~10-65 seconds per coaching response
- **Throughput**: Handles multiple concurrent conversations

## Next Steps

1. ✅ WhatsApp integrated
2. ✅ QR code in dashboard
3. ✅ Message routing working
4. ⏳ Production deployment
5. ⏳ Scale to multiple instances

---

**Status**: WhatsApp fully integrated and ready to use! 🚀
