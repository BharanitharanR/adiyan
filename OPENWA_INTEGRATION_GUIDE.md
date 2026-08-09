# OpenWA Integration Guide for Adiyan Orchestrator

**Purpose**: Replace direct whatsapp-web.js with OpenWA API  
**Effort**: ~2-3 days for Phase 1 & 2  
**Risk**: Low (OpenWA runs parallel, no cutover required)  

---

## Architecture Change

### Before (Current)
```
WhatsApp User
    ↓
Node.js (app.js with whatsapp-web.js)
    ↓
RabbitMQ (messages.incoming)
    ↓
Python Orchestrator (7 agents)
    ↓ ParserAgent → ValidatorAgent → RouterAgent → LLMAgent → SynthesizerAgent → StorageAgent → PublisherAgent
    ↓
PublisherAgent calls: node_handler._send_whatsapp_reply()
    ↓
Node.js sends back via WhatsApp Web
    ↓
WhatsApp User
```

**Problems**:
- Direct library coupling
- Manual contact name matching (failing)
- Response delivery unreliable
- No operational visibility

### After (OpenWA)
```
WhatsApp User
    ↓
OpenWA (HTTP API, port 2785)
    ↓
Webhook → http://localhost:5001/webhook/openwa
    ↓
Python Orchestrator (7 agents)
    ↓ ParserAgent → ValidatorAgent → RouterAgent → LLMAgent → SynthesizerAgent → StorageAgent → PublisherAgent
    ↓
PublisherAgent calls: requests.post("http://localhost:2785/api/sessions/{id}/message/send")
    ↓
OpenWA API → WhatsApp Web
    ↓
WhatsApp User
```

**Benefits**:
- Clean HTTP API separation
- Reliable message delivery
- Dashboard monitoring
- Built-in rate limiting
- Media file support

---

## Step 1: Setup OpenWA (Already Done ✓)

```bash
cd /Users/bharani/Desktop/aiAgentCompaction/Adiyan/penwa

# Verify it's running
curl http://localhost:2785/api/health
# {"status":"ok","version":"0.14.6"}
```

**Next**: Create API key via dashboard at http://localhost:2785

---

## Step 2: Create Webhook Receiver in Adiyan

Create: `/Users/bharani/Desktop/aiAgentCompaction/Adiyan/services/openwa_webhook.py`

```python
"""
OpenWA Webhook Receiver
Receives real-time messages from OpenWA and converts to AgentState
"""

from flask import Blueprint, request, jsonify
import json
import uuid
from datetime import datetime
import pika
import logging

logger = logging.getLogger('OpenWAWebhook')

openwa_bp = Blueprint('openwa_webhook', __name__)

# Store for session ID mapping (OpenWA → Adiyan)
OPENWA_SESSIONS = {}

class OpenWAAdapter:
    """Convert OpenWA webhook events to Adiyan AgentState"""
    
    @staticmethod
    def webhook_to_message(webhook_data: dict) -> dict:
        """
        Convert OpenWA webhook format to Adiyan message format
        
        OpenWA webhook:
        {
            "event": "message.received",
            "data": {
                "sessionId": "adiyan-coaching",
                "messageId": "3EB0XXX@c.us",
                "fromNumber": "1234567890",
                "chatId": "1234567890@c.us",
                "body": "How do I improve my productivity?",
                "senderName": "Sripriya",
                "timestamp": 1691234567890,
                "isGroup": false,
                "hasMedia": false
            }
        }
        """
        
        event_type = webhook_data.get('event')
        
        # Only process message.received events
        if event_type != 'message.received':
            return None
        
        data = webhook_data.get('data', {})
        
        # Extract fields
        session_id = data.get('sessionId')
        message_id = data.get('messageId')
        contact_name = data.get('senderName', data.get('fromNumber', 'Unknown'))
        chat_id = data.get('chatId')
        message_body = data.get('body', '')
        timestamp = data.get('timestamp')
        
        # Convert to Adiyan format
        return {
            'message_id': str(uuid.uuid4()),  # Generate new ID for orchestrator
            'contact_name': contact_name,
            'lid': chat_id,  # Local ID (WhatsApp chat ID)
            'message_body': message_body,
            'is_user_message': True,
            'timestamp': timestamp,
            'session_id': session_id,  # Store OpenWA session ID for reply
            'whatsapp_message_id': message_id  # Store original message ID
        }
    
    @staticmethod
    def publish_to_rabbitmq(message_data: dict):
        """Publish converted message to RabbitMQ"""
        try:
            connection = pika.BlockingConnection(
                pika.URLParameters('amqp://guest:guest@localhost/')
            )
            channel = connection.channel()
            channel.exchange_declare('adiyan', 'topic', durable=True)
            
            channel.basic_publish(
                exchange='adiyan',
                routing_key='messages.incoming',
                body=json.dumps(message_data),
                properties=pika.BasicProperties(delivery_mode=2)
            )
            
            connection.close()
            logger.info(f"Published message {message_data['message_id']} to RabbitMQ")
            
        except Exception as e:
            logger.error(f"Failed to publish to RabbitMQ: {e}")
            raise


@openwa_bp.route('/webhook/openwa', methods=['POST'])
def receive_webhook():
    """Receive OpenWA webhook events"""
    try:
        webhook_data = request.get_json()
        
        logger.info(f"📬 Received OpenWA webhook: {webhook_data.get('event')}")
        
        # Verify signature (if enabled)
        # signature = request.headers.get('X-OpenWA-Signature')
        # if not verify_hmac_signature(request.data, signature):
        #     return jsonify({'error': 'Invalid signature'}), 401
        
        # Convert to Adiyan format
        adapter = OpenWAAdapter()
        message = adapter.webhook_to_message(webhook_data)
        
        if not message:
            # Ignore non-message events
            return jsonify({'status': 'ignored'}), 200
        
        # Publish to RabbitMQ
        adapter.publish_to_rabbitmq(message)
        
        return jsonify({
            'status': 'received',
            'message_id': message['message_id']
        }), 202
        
    except Exception as e:
        logger.error(f"❌ Webhook processing error: {e}")
        return jsonify({'error': str(e)}), 500


def register_openwa_webhook(api_key: str, flask_host: str = 'localhost', flask_port: int = 5001):
    """
    Register webhook with OpenWA server
    Call this once at startup to configure OpenWA to send webhooks to Adiyan
    """
    import requests
    
    webhook_url = f"http://{flask_host}:{flask_port}/webhook/openwa"
    
    try:
        response = requests.post(
            'http://localhost:2785/api/webhooks',
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json'
            },
            json={
                'url': webhook_url,
                'events': ['message.received', 'message.status'],
                'signature_type': 'hmac_sha256'
            }
        )
        
        if response.status_code in [200, 201]:
            logger.info(f"✅ Webhook registered with OpenWA: {webhook_url}")
            return response.json()
        else:
            logger.error(f"❌ Failed to register webhook: {response.status_code}")
            logger.error(response.text)
            return None
            
    except Exception as e:
        logger.error(f"❌ Failed to register webhook: {e}")
        return None
```

---

## Step 3: Update Flask Control Panel

Add to: `/Users/bharani/Desktop/aiAgentCompaction/Adiyan/ui/control_panel_api.py`

```python
from services.openwa_webhook import openwa_bp, register_openwa_webhook

# Register OpenWA blueprint
app.register_blueprint(openwa_bp)

# At startup, register webhook with OpenWA
@app.before_request
def startup_setup():
    """Run once at startup"""
    if not hasattr(app, '_openwa_webhook_registered'):
        try:
            api_key = os.getenv('OPENWA_API_KEY')
            if api_key:
                register_openwa_webhook(
                    api_key,
                    flask_host=os.getenv('FLASK_HOST', 'localhost'),
                    flask_port=5001
                )
            app._openwa_webhook_registered = True
        except Exception as e:
            logger.error(f"Failed to setup OpenWA webhook: {e}")
```

---

## Step 4: Update PublisherAgent for OpenWA

Modify: `/Users/bharani/Desktop/aiAgentCompaction/Adiyan/agents/publisher_agent.py`

```python
import requests

class PublisherAgent(BaseAgent):
    """Agent 7: Publish response back to WhatsApp and RabbitMQ"""

    def __init__(self, config: Dict[str, Any] = None, whatsapp_sender: Optional[Callable] = None):
        tools = ['send_whatsapp_reply', 'publish_event', 'log_completion']
        super().__init__('PublisherAgent', tools, config)
        
        # Support both old (Node.js) and new (OpenWA) backends
        self.use_openwa = config.get('use_openwa', False)
        self.openwa_url = config.get('openwa_url', 'http://localhost:2785')
        self.openwa_api_key = config.get('openwa_api_key', '')
        self.rabbitmq_url = config.get('rabbitmq_url', 'amqp://guest:guest@localhost/')
        self.whatsapp_sender = whatsapp_sender

    async def execute(self, state: AgentState) -> AgentState:
        """Publish response"""
        try:
            # Handle error case
            if state.error:
                self.log_stage(f"Pipeline error: {state.error}", 'error')
                error_msg = f"❌ {state.error}"
                if self.use_openwa:
                    await self._send_via_openwa(state, error_msg)
                else:
                    await self._send_response(state, error_msg)
                await self._publish_event(state, 'error')
                return state

            # Handle registration
            if state.is_registration:
                response = f"✅ Registered: {state.contact_name}\n\nYou can now ask me for coaching on productivity, goal-setting, and personal development!"
                if self.use_openwa:
                    await self._send_via_openwa(state, response)
                else:
                    await self._send_response(state, response)
                await self._publish_event(state, 'registered')
                self.log_stage(f"✅ Registration complete: {state.contact_name}")
                return state

            # Handle unregistration
            if state.is_unregistration:
                response = f"✅ Unregistered: {state.contact_name}"
                if self.use_openwa:
                    await self._send_via_openwa(state, response)
                else:
                    await self._send_response(state, response)
                await self._publish_event(state, 'unregistered')
                self.log_stage(f"✅ Unregistration complete: {state.contact_name}")
                return state

            # Send coaching response (join chunks)
            if state.metadata.get('chunks'):
                chunks = state.metadata['chunks']
                full_response = '\n'.join(chunks)
                if self.use_openwa:
                    await self._send_via_openwa(state, full_response)
                else:
                    await self._send_response(state, full_response)
                await self._publish_event(state, 'response_sent')
                self.log_stage(f"✅ Response sent ({len(chunks)} chunk(s))")
                return state

            # No response to send
            self.log_stage(f"⚠️  No response to send", 'warning')
            return state

        except Exception as e:
            self.log_stage(f"❌ Publish failed: {str(e)}", 'error')
            return state

    async def _send_via_openwa(self, state: AgentState, response: str):
        """Send response via OpenWA API"""
        try:
            session_id = state.metadata.get('session_id', 'adiyan-coaching')
            chat_id = state.metadata.get('lid', f'{state.contact_name}@c.us')
            
            # Ensure chat_id is in WhatsApp format
            if not chat_id.endswith('@c.us'):
                # Try to extract phone number and format it
                phone = state.metadata.get('fromNumber', '')
                if phone:
                    chat_id = f"{phone}@c.us"
            
            payload = {
                'chatId': chat_id,
                'message': response,
                'quotedMessageId': None
            }
            
            resp = requests.post(
                f'{self.openwa_url}/api/sessions/{session_id}/message/send',
                headers={
                    'Authorization': f'Bearer {self.openwa_api_key}',
                    'Content-Type': 'application/json'
                },
                json=payload,
                timeout=10
            )
            
            if resp.status_code in [200, 201]:
                self.log_stage(f"📤 Sent via OpenWA: {state.contact_name}")
            else:
                self.log_stage(f"❌ OpenWA send failed: {resp.status_code} - {resp.text}", 'error')
        
        except Exception as e:
            self.log_stage(f"❌ OpenWA send error: {str(e)}", 'error')

    async def _send_response(self, state: AgentState, response: str):
        """Send via RabbitMQ (fallback to old method)"""
        # ... existing code ...

    async def _publish_event(self, state: AgentState, event_type: str):
        """Publish event to RabbitMQ"""
        # ... existing code ...
```

---

## Step 5: Update Configuration

Add to: `/Users/bharani/Desktop/aiAgentCompaction/Adiyan/config/control_plane.py`

```python
# Add OpenWA configuration
class Config:
    use_openwa = bool(os.getenv('USE_OPENWA', 'false').lower() == 'true')
    openwa_url = os.getenv('OPENWA_URL', 'http://localhost:2785')
    openwa_api_key = os.getenv('OPENWA_API_KEY', '')
    # ... rest of config ...
```

Add to `.env`:
```bash
# OpenWA Integration
USE_OPENWA=true
OPENWA_URL=http://localhost:2785
OPENWA_API_KEY=sk_live_your_api_key_here
```

---

## Step 6: Test E2E Flow

### 6a. Setup OpenWA Session

```bash
# 1. Get API key from dashboard http://localhost:2785
API_KEY="sk_live_xxxxxxxxxxxx"

# 2. Create session
curl -X POST http://localhost:2785/api/sessions \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "adiyan-coaching"}'

# Response:
# {
#   "sessionId": "adiyan-coaching",
#   "status": "created",
#   "createdAt": "2026-08-08T12:00:00Z"
# }

# 3. Get QR code
curl -H "Authorization: Bearer $API_KEY" \
  http://localhost:2785/api/sessions/adiyan-coaching/qr | jq -r '.qr' > qr.txt

# 4. Scan QR code from WhatsApp Mobile to authenticate
```

### 6b. Register Webhook

```bash
API_KEY="sk_live_xxxxxxxxxxxx"

curl -X POST http://localhost:2785/api/webhooks \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "http://localhost:5001/webhook/openwa",
    "events": ["message.received", "message.status"],
    "signature_type": "hmac_sha256"
  }'
```

### 6c. Start Adiyan

```bash
cd /Users/bharani/Desktop/aiAgentCompaction/Adiyan
python main.py
```

Check logs:
```
✅ OpenWA webhook registered: http://localhost:5001/webhook/openwa
✅ All 7 agents initialized
✅ Ready for messages!
```

### 6d. Send Test Message from WhatsApp

From your WhatsApp mobile app (the authenticated number):
- Send message to the coaching number/group: "How do I improve my productivity?"

### 6e. Verify Flow

```bash
# Check Adiyan logs
tail -f ~/.Adiyan/orchestrator.log

# Expected:
# 📬 Received OpenWA webhook: message.received
# 📨 Message from Sripriya: How do i improve my productivity...
# ✅ Whitelisted: Sripriya
# 📤 Published to RabbitMQ (ID: xxx)
# [ParserAgent] Message parsed
# [ValidatorAgent] Message validated
# ...
# [LLMAgent] LLM response ready
# ...
# [PublisherAgent] Sent via OpenWA: Sripriya

# Check OpenWA dashboard
# http://localhost:2785 → Sessions → Messages tab
```

---

## Step 7: Optional - Add Media Support

Send coaching materials:

```python
import base64
import requests

def send_coaching_pdf(api_key: str, session_id: str, contact_id: str, pdf_path: str):
    """Send coaching materials PDF"""
    
    with open(pdf_path, 'rb') as f:
        media_b64 = base64.b64encode(f.read()).decode()
    
    response = requests.post(
        f'http://localhost:2785/api/sessions/{session_id}/message/send-media',
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json'
        },
        json={
            'chatId': f'{contact_id}@c.us',
            'mediaFile': media_b64,
            'fileName': 'coaching_guide.pdf',
            'caption': '📚 Your personalized coaching materials',
            'mimeType': 'application/pdf'
        }
    )
    
    return response.json()

# Usage in PublisherAgent
if state.metadata.get('media_file'):
    send_coaching_pdf(
        api_key=self.openwa_api_key,
        session_id='adiyan-coaching',
        contact_id=state.contact_name,
        pdf_path=state.metadata['media_file']
    )
```

---

## Rollback Plan

If issues occur, revert to old system:

```python
# In config
USE_OPENWA=false

# PublisherAgent automatically uses old RabbitMQ method
# No code changes required, just config flag
```

---

## Success Criteria

- ✅ OpenWA running at port 2785
- ✅ Webhook registered and receiving messages
- ✅ Messages flow: WhatsApp → OpenWA → Adiyan → Ollama → OpenWA → WhatsApp
- ✅ No manual contact matching (OpenWA handles it)
- ✅ Dashboard shows session status and message logs
- ✅ Rate limiting active (prevents WhatsApp bans)

---

## Timeline

| Phase | Task | Duration |
|-------|------|----------|
| **1** | Create webhook receiver | 2-4 hours |
| **1** | Update control panel & config | 1-2 hours |
| **2** | Update PublisherAgent | 1-2 hours |
| **2** | E2E testing & debugging | 4-8 hours |
| **3** | Production migration | 2-4 hours |
| **3** | Rollback testing | 1-2 hours |

**Total**: ~2-3 days for Phase 1 & 2

---

## Support Resources

- OpenWA Docs: https://open-wa.github.io/
- OpenWA API: http://localhost:2785/api-docs
- Dashboard: http://localhost:2785
- Test scripts: `/Users/bharani/Desktop/aiAgentCompaction/Adiyan/penwa/test-*.js`

---

**Status**: Ready for Phase 1 implementation  
**Next**: Create webhook receiver (Step 2)
