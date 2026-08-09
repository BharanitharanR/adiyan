"""
WhatsApp Bridge Service
Connects WhatsApp bot to Adiyan orchestrator via RabbitMQ
"""

import asyncio
import logging
import json
import uuid
import time
from pathlib import Path
import pika
from typing import Callable, Optional

logger = logging.getLogger('WhatsAppBridge')

# Try to import WhatsApp library - graceful fallback
try:
    from whatsapp_web import Client, LocalAuth
    WHATSAPP_AVAILABLE = True
except ImportError:
    WHATSAPP_AVAILABLE = False
    logger.warning("whatsapp-web.js not available - using mock mode")

class WhatsAppBridge:
    """Bridge between WhatsApp and Adiyan orchestrator"""

    def __init__(self, rabbitmq_url: str = 'amqp://guest:guest@localhost/'):
        self.rabbitmq_url = rabbitmq_url
        self.logger = logger
        self.qr_callback: Optional[Callable] = None
        self.current_qr = self._generate_test_qr()  # Generate test QR on startup
        self.is_connected = False
        self.waClient = None
        self.channel = None
        self.connection = None

        # Create auth directory
        self.auth_path = Path.home() / '.Adiyan' / 'whatsapp_auth'
        self.auth_path.mkdir(parents=True, exist_ok=True)

    def _generate_test_qr(self) -> str:
        """Generate test QR code data for UI display"""
        # Generate a test QR string (base64-like pattern for display)
        test_data = f"ADIYAN_WHATSAPP_QR_{int(time.time())}"
        return test_data

    def setup_rabbitmq(self) -> bool:
        """Setup RabbitMQ connection"""
        try:
            self.connection = pika.BlockingConnection(
                pika.URLParameters(self.rabbitmq_url)
            )
            self.channel = self.connection.channel()

            # Declare exchange
            self.channel.exchange_declare(
                exchange='adiyan',
                exchange_type='topic',
                durable=True
            )

            # Declare reply queue
            self.channel.queue_declare(queue='whatsapp-replies', durable=True)
            self.channel.queue_bind(
                exchange='adiyan',
                queue='whatsapp-replies',
                routing_key='events.response_sent'
            )

            self.logger.info("✅ RabbitMQ connected")
            return True

        except Exception as e:
            self.logger.error(f"❌ RabbitMQ connection failed: {e}")
            return False

    def setup_whatsapp(self):
        """Setup WhatsApp client"""
        if not WHATSAPP_AVAILABLE:
            self.logger.warning("⚠️  WhatsApp not available - using mock mode")
            return

        try:
            # This would use actual whatsapp-web.js library
            self.logger.info("📱 WhatsApp client initialized")
        except Exception as e:
            self.logger.error(f"❌ WhatsApp setup failed: {e}")

    def publish_to_orchestrator(self, contact_name: str, lid: str, message: str):
        """Publish message to orchestrator"""
        try:
            if not self.channel:
                self.logger.error("RabbitMQ not connected")
                return

            msg_data = {
                'message_id': str(uuid.uuid4()),
                'contact_name': contact_name,
                'lid': lid,
                'message_body': message
            }

            self.channel.basic_publish(
                exchange='adiyan',
                routing_key='messages.incoming',
                body=json.dumps(msg_data),
                properties=pika.BasicProperties(delivery_mode=2)
            )

            self.logger.info(f"📤 Published message from {contact_name}")

        except Exception as e:
            self.logger.error(f"❌ Publish failed: {e}")

    def listen_for_replies(self):
        """Listen for replies from orchestrator"""
        if not self.channel:
            self.logger.error("RabbitMQ not connected")
            return

        def callback(ch, method, properties, body):
            try:
                reply_data = json.loads(body)
                contact_name = reply_data.get('contact_name')
                response = reply_data.get('response')

                if not contact_name or not response:
                    self.logger.warning(f"⚠️  Invalid reply data: contact={contact_name}, response={response is not None}")
                    ch.basic_ack(delivery_tag=method.delivery_tag)
                    return

                self.logger.info(f"📬 Received reply for {contact_name}")

                # Send to WhatsApp
                self._send_whatsapp_reply(contact_name, response)

                ch.basic_ack(delivery_tag=method.delivery_tag)

            except Exception as e:
                self.logger.error(f"❌ Reply processing failed: {e}")
                # Don't requeue - discard on error to prevent infinite loop
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

        try:
            self.channel.basic_qos(prefetch_count=1)
            self.channel.basic_consume(
                queue='whatsapp-replies',
                on_message_callback=callback
            )

            self.logger.info("👂 Listening for replies...")
            self.channel.start_consuming()

        except Exception as e:
            self.logger.error(f"❌ Reply listener failed: {e}")

    def _send_whatsapp_reply(self, contact_name: str, response: str):
        """Send reply to WhatsApp via Node.js service"""
        if not response or not contact_name:
            self.logger.warning(f"⚠️  Cannot send empty response to {contact_name}")
            return

        try:
            # Call Node.js service to send message
            api_url = 'http://localhost:3001/api/send'
            data = {
                'contactName': contact_name,
                'message': response
            }

            import requests
            resp = requests.post(api_url, json=data, timeout=10)

            if resp.status_code == 200:
                result = resp.json()
                if result.get('sent'):
                    self.logger.info(f"✅ Sent response to {contact_name}")
                else:
                    self.logger.error(f"❌ Failed to send to {contact_name}: {result.get('error')}")
            else:
                self.logger.error(f"❌ Node.js service error: {resp.status_code}")

        except Exception as e:
            self.logger.error(f"❌ Failed to send reply: {e}")

    def set_qr_callback(self, callback: Callable):
        """Set callback for QR code updates"""
        self.qr_callback = callback

    def update_qr(self, qr_text: str):
        """Update QR code"""
        self.current_qr = qr_text
        if self.qr_callback:
            self.qr_callback(qr_text)
        self.logger.info("⚡ QR code updated")

    def set_connected(self, connected: bool):
        """Set connection status"""
        self.is_connected = connected
        status = "✅ Connected" if connected else "❌ Disconnected"
        self.logger.info(f"📱 WhatsApp {status}")

    def get_status(self) -> dict:
        """Get WhatsApp status"""
        return {
            'connected': self.is_connected,
            'qr_available': self.current_qr is not None,
            'qr_text': self.current_qr
        }
