"""
OpenWA Webhook Receiver
Receives messages from OpenWA and feeds them into the orchestrator
"""

import json
import logging
from typing import Dict, Any, Callable
import uuid
from datetime import datetime

logger = logging.getLogger('OpenWAReceiver')


class OpenWAAdapter:
    """Convert OpenWA webhook events to Adiyan AgentState format"""

    @staticmethod
    def webhook_to_message(webhook_data: dict) -> dict:
        """
        Convert OpenWA webhook format to Adiyan message format

        Input (OpenWA webhook):
        {
            "event": "message.received",
            "data": {
                "sessionId": "adiyan-coaching",
                "messageId": "3EB0XXX@c.us",
                "fromNumber": "919080089081",
                "senderName": "Sripriya",
                "chatId": "919080089081@c.us",
                "body": "How do I improve my productivity?",
                "timestamp": 1691234567890,
                "isGroup": false,
                "hasMedia": false
            }
        }

        Output (Adiyan format):
        {
            "message_id": "uuid",
            "contact_name": "Sripriya",
            "lid": "919080089081@c.us",
            "message_body": "How do I improve my productivity?",
            "is_user_message": true,
            "timestamp": 1691234567890,
            "session_id": "adiyan-coaching",
            "whatsapp_message_id": "3EB0XXX@c.us"
        }
        """

        event_type = webhook_data.get('event')

        # Only process message.received events
        if event_type != 'message.received':
            logger.debug(f"Ignoring event type: {event_type}")
            return None

        data = webhook_data.get('data', {})

        # Extract fields
        session_id = data.get('sessionId')
        message_id = data.get('messageId')
        contact_name = data.get('senderName', data.get('fromNumber', 'Unknown'))
        chat_id = data.get('chatId')
        message_body = data.get('body', '')
        timestamp = data.get('timestamp')
        from_number = data.get('fromNumber', '')

        # Ignore messages without body
        if not message_body or not message_body.strip():
            logger.debug("Ignoring empty message")
            return None

        # Convert to Adiyan format
        adiyan_message = {
            'message_id': str(uuid.uuid4()),  # Generate new ID for orchestrator
            'contact_name': contact_name,
            'lid': chat_id,  # Local ID (WhatsApp chat ID)
            'message_body': message_body.strip(),
            'is_user_message': True,  # Mark as user message to prevent reprocessing
            'timestamp': timestamp,
            'session_id': session_id,  # Store OpenWA session ID for reply
            'whatsapp_message_id': message_id,  # Store original message ID
            'from_number': from_number  # Store phone number for identification
        }

        logger.info(f"✅ Converted webhook from {contact_name}: {message_body[:50]}...")
        return adiyan_message

    @staticmethod
    def polled_message_to_adiyan(message: dict, session_name: str) -> dict:
        """
        Convert a message returned by GET /api/sessions/{id}/messages (poller path)
        into Adiyan's internal message format. Shape differs from the webhook payload:

        Input (OpenWA message record):
        {
            "id": "8c5c963f-...",
            "waMessageId": "false_213103828537359@lid_...",
            "chatId": "213103828537359@lid",
            "chatName": "Sripriya",
            "from": "213103828537359@lid",
            "body": "How do I improve my productivity?",
            "direction": "incoming",
            "timestamp": 1786242935
        }
        """
        message_body = (message.get('body') or '').strip()
        if not message_body:
            return None

        contact_name = message.get('chatName') or message.get('from', 'Unknown')

        return {
            'message_id': str(uuid.uuid4()),
            'contact_name': contact_name,
            'lid': message.get('chatId'),
            'message_body': message_body,
            'is_user_message': True,
            'timestamp': message.get('timestamp'),
            'session_id': session_name,
            'whatsapp_message_id': message.get('waMessageId') or message.get('id'),
            'from_number': (message.get('from') or '').split('@')[0],
        }


class OpenWAReceiver:
    """Receives OpenWA webhooks and processes through orchestrator"""

    def __init__(self, orchestrator_callback: Callable):
        """
        Initialize receiver

        Args:
            orchestrator_callback: Async function to process message through orchestrator
                                   Should accept (message_dict) and return result
        """
        self.orchestrator_callback = orchestrator_callback
        self.adapter = OpenWAAdapter()
        logger.info("✅ OpenWA Receiver initialized")

    async def process_webhook(self, webhook_data: dict) -> dict:
        """
        Process OpenWA webhook event

        Args:
            webhook_data: Raw webhook payload from OpenWA

        Returns:
            Response dict with status and result
        """
        try:
            logger.info(f"📬 Received OpenWA webhook: {webhook_data.get('event')}")

            # Convert webhook to Adiyan message format
            message = self.adapter.webhook_to_message(webhook_data)

            if not message:
                logger.debug("Webhook ignored (not a message.received event)")
                return {'status': 'ignored', 'reason': 'not_message_received'}

            # Log incoming message
            logger.info(f"📨 Message from {message['contact_name']}: {message['message_body'][:100]}")

            # Process through orchestrator
            try:
                result = await self.orchestrator_callback(message)

                logger.info(f"✅ Message processed for {message['contact_name']}")

                return {
                    'status': 'processed',
                    'message_id': message['message_id'],
                    'contact_name': message['contact_name'],
                    'result': result
                }

            except Exception as e:
                logger.error(f"❌ Orchestrator processing failed: {str(e)}")
                return {
                    'status': 'error',
                    'message_id': message['message_id'],
                    'error': str(e)
                }

        except Exception as e:
            logger.error(f"❌ Webhook processing error: {str(e)}")
            return {
                'status': 'error',
                'error': str(e)
            }

    async def verify_signature(self, raw_body: bytes, signature: str, secret: str) -> bool:
        """
        Verify OpenWA webhook signature (HMAC-SHA256)

        Args:
            raw_body: Raw HTTP request body
            signature: X-OpenWA-Signature header value
            secret: Webhook secret configured in OpenWA

        Returns:
            True if signature is valid
        """
        import hmac
        import hashlib

        expected = 'sha256=' + hmac.new(
            secret.encode('utf-8'),
            raw_body,
            hashlib.sha256
        ).hexdigest()

        is_valid = hmac.compare_digest(signature, expected)

        if is_valid:
            logger.debug("✅ Webhook signature verified")
        else:
            logger.warning("❌ Webhook signature invalid")

        return is_valid
