"""
OpenWA Webhook Receiver
Receives messages from OpenWA and feeds them into the orchestrator
"""

import json
import logging
from typing import Dict, Any, Callable
import uuid
from datetime import datetime

from mesh.lib.utilities import watermark

logger = logging.getLogger('OpenWAReceiver')


def _strip_device_suffix(jid: str) -> str:
    """<user>:<device_id>@domain -> <user>@domain. Confirmed live that
    `author` on a message.sent event sometimes carries a WhatsApp
    multi-device suffix (e.g. '6503050272861:14@lid') that `chatId` never
    does for the same identity - comparing the raw strings then silently
    evaluates unequal for a genuine self-chat, same failure shape as the
    from-vs-to JID-form mismatch this replaced. Both sides of any identity
    comparison should go through this, not just one, in case chatId ever
    carries a suffix too."""
    if not jid or '@' not in jid:
        return jid
    user, domain = jid.split('@', 1)
    return f"{user.split(':', 1)[0]}@{domain}"


class OpenWAAdapter:
    """Convert OpenWA webhook events to Adiyan AgentState format"""

    @staticmethod
    def webhook_to_message(webhook_data: dict) -> dict:
        """
        Convert OpenWA webhook format to Adiyan message format.

        Input (real OpenWA wire shape - WebhookPayload in
        penwa/src/modules/webhook/webhook.service.ts, data fields from
        IncomingMessage in penwa/src/engine/interfaces/whatsapp-engine.interface.ts.
        Previously this docstring and the code below assumed a `senderName`/
        `fromNumber`/data-nested-`sessionId` shape that has never existed in this
        fork - contact_name silently fell back to 'Unknown' and from_number to ''
        on every real webhook call as a result, verified via the actual TS
        source, not assumption):
        {
            "event": "message.received",
            "sessionId": "adiyan",
            "data": {
                "id": "3EB0XXX@c.us",
                "from": "919080089081@c.us",
                "to": "...",
                "chatId": "919080089081@c.us",
                "author": null,
                "body": "How do I improve my productivity?",
                "timestamp": 1691234567890,
                "fromMe": false,
                "isGroup": false,
                "kind": "individual",
                "contact": {"pushName": "Sripriya", "name": "Sripriya"}
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
            "session_id": "adiyan",
            "whatsapp_message_id": "3EB0XXX@c.us",
            "from_number": "919080089081"
        }
        """

        event_type = webhook_data.get('event')

        # message.received: a real inbound message (OpenWA's own engine never
        # fires this for fromMe - see message-projector.service.ts's
        # onMessage, which returns early on message.fromMe before this event
        # is even considered).
        #
        # message.sent: fires for every account-composed outgoing send -
        # both ones sent via OpenWAService.send_message (this codebase's own
        # replies, always carrying the watermark - see openwa_service.py) AND
        # ones the owner types by hand into WhatsApp on their own linked
        # phone (never watermarked, since watermark.apply() only runs inside
        # send_message). The watermark is exactly the signal that tells the
        # two apart: a watermarked message.sent is Adiyan's own reply and
        # must be dropped here, or every reply would re-enter the pipeline as
        # if it were new input and loop forever. An unwatermarked one is the
        # owner talking to themselves (self-chat) or to someone else from
        # their own phone - genuine, not an echo - and is let through the
        # same as a real message.received, so a self-chat message can work
        # as an owner-only command channel (fromMe messages have no other
        # path into this pipeline at all).
        if event_type not in ('message.received', 'message.sent'):
            logger.debug(f"Ignoring event type: {event_type}")
            return None

        data = webhook_data.get('data', {})

        if event_type == 'message.sent':
            if not data.get('fromMe'):
                # Should be impossible (message.sent only fires for fromMe
                # sends - see handleOwnSendEcho), but this is the exact
                # condition the echo-loop guard depends on, so it's checked
                # explicitly rather than assumed.
                logger.debug("Ignoring message.sent with fromMe=False (unexpected)")
                return None
            if watermark.has_watermark(data.get('body', '')):
                logger.debug("Ignoring message.sent that carries our own watermark (self-echo)")
                return None

        # Only 1:1 conversations - not channel/newsletter broadcasts, status
        # updates, groups, or anything else OpenWA's own `kind` classifier
        # can't place. `kind` is derived server-side from chatId (penwa/src/
        # engine/identity/wa-id.ts's ChatKind) - reliable, unlike
        # pattern-matching the JID ourselves. This is what let a WhatsApp
        # Channel broadcast get treated as a real inbound chat message and
        # routed/replied-to (confirmed live: a news channel post got answered
        # back into the channel itself).
        #
        # Groups are excluded outright, not routed like a 1:1 - confirmed
        # live to matter: with no group exclusion, every message any group
        # member sent (registered or not) got a reply posted back into the
        # group, and the owner's own ordinary group chatter got misread as an
        # owner command and answered into the group too, since is_owner()
        # runs before anything else knows this was a group message. A group
        # chat_id is never a legitimate 1:1 target for Adiyan's replies, so
        # this is excluded before rules_engine ever sees it - same behavior
        # the retired openwa_poller.py had (it skipped every `@g.us` chat_id
        # unconditionally), dropped by mistake in this port and restored here.
        kind = data.get('kind')
        if kind == 'group':
            logger.info("Ignoring group message - Adiyan only responds in 1:1 chats")
            return None
        if kind != 'individual':
            logger.info(f"Ignoring non-conversation webhook (kind={kind!r})")
            return None

        # sessionId is top-level on the webhook payload, not nested in data.
        session_id = webhook_data.get('sessionId')
        message_id = data.get('id')
        chat_id = data.get('chatId')
        message_body = data.get('body', '')
        timestamp = data.get('timestamp')

        contact = data.get('contact') or {}
        contact_name = contact.get('pushName') or contact.get('name') or 'Unknown'
        logger.info(f"Received webhook from {contact_name}: {contact.get('pushName') or contact.get('name') or '[no name]'}: {message_body[:50]}...")
        # Sender's raw JID - the group participant (`author`) for a group
        # message, the chat itself (`from`) for a 1:1. Only a `@c.us` (phone-
        # addressed) JID actually yields a real phone number; an unresolved
        # `@lid` privacy id does not (see wa-id.ts) - from_number is left
        # empty rather than smuggling a lid number through as if it were one.
        #
        # Confirmed live to matter for a self-chat specifically: OpenWA
        # reports `from` inconsistently there - sometimes the real
        # `<phone>@c.us`, sometimes the same `@lid` the chat itself is
        # addressed by (chatId and `from`/`to` all equal, no phone-form JID
        # anywhere in the payload) - so this parse silently produced ''
        # for a genuine self-sent message and broke owner recognition. The
        # `from_me` flag below is the reliable signal for that case instead
        # of trying to parse a JID WhatsApp itself reports inconsistently.
        sender_jid = data.get('author') or data.get('from') or ''
        from_number = sender_jid.split('@')[0] if sender_jid.endswith('@c.us') else ''

        # An image or document carries no body text - pass its media
        # through instead of dropping it on the empty-body check below.
        # `data` when inlined (<=1MiB, WEBHOOK_MEDIA_INLINE_MAX_BYTES); None
        # + omitted=True when OpenWA dropped the blob and the caller must
        # fetch it separately via OpenWAService.download_media(chat_id,
        # whatsapp_message_id).
        #
        # 'document' (a real file upload via WhatsApp's attachment picker,
        # not a photo) is captured the same way as 'image' - confirmed live
        # that without this, a coach uploading an actual PDF got silently
        # dropped here entirely (no media captured, no body text either,
        # so it never even reached the empty-message check as anything).
        # `kind` carries which one this was through to WhatsApp MCP's own
        # webhook handler, since that's where the two start being treated
        # differently (mesh/mcp/whatsapp/server.py's handle_webhook splits
        # them back into separate `image`/`document` A2A call params).
        # `filename` is only ever populated for a document - WhatsApp never
        # gives an image one.
        media = None
        raw_type = data.get('type')
        if raw_type in ('image', 'document') and data.get('media'):
            raw_media = data['media']
            media = {
                'kind': raw_type,
                'mimetype': raw_media.get('mimetype'),
                'data': raw_media.get('data'),
                'omitted': bool(raw_media.get('omitted')),
                'filename': raw_media.get('filename') if raw_type == 'document' else None,
            }

        # Ignore messages without body or media
        if not media and (not message_body or not message_body.strip()):
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
            'from_number': from_number,  # Store phone number for identification
            'media': media,  # None for a plain text message
            # message.sent only ever fires for the account's own outgoing
            # sends (see the docstring above) - unconditionally true here,
            # never parsed from a JID. The one reliable owner signal
            # regardless of which JID form `from` happened to report.
            'from_me': event_type == 'message.sent',
            # True only for a genuine self-chat send (you messaging
            # yourself), never for a message you send to someone else from
            # your own phone. chatId == author (normalized), NOT from == to -
            # confirmed live that `from` is reported in phone form
            # (<phone>@c.us) while `to`/`chatId`/`author` are all reported in
            # lid form for the exact same self-chat, so from == to silently
            # and permanently evaluated False for every self-chat send.
            # `author` is reliable instead: confirmed live it's always your
            # own lid identity on a message.sent event regardless of
            # destination chat, so chatId == author is true exactly when the
            # destination chat *is* your own identity - i.e. self-chat. Both
            # sides go through _strip_device_suffix() - confirmed live that
            # `author` sometimes carries a multi-device suffix (e.g.
            # '...:14@lid') that chatId never does, which silently broke the
            # raw-string comparison the same way from == to did originally.
            # Deliberately not resolved via get_own_chat_id(), which is
            # confirmed live to hang indefinitely. Meaningless (always False)
            # for message.received, since only the account's own outgoing
            # sends can be a self-chat command in the first place.
            'is_self_chat': (
                event_type == 'message.sent'
                and _strip_device_suffix(data.get('chatId')) == _strip_device_suffix(data.get('author'))
            ),
        }

        # TEMP DIAGNOSTIC round 2 - a self-chat + @Adiyan message still went
        # silent after the chatId==author fix, despite that fix being
        # verified correct against an earlier captured payload. Logging the
        # raw fields again to see whether *this* message's payload actually
        # differs (author missing/null this time, different kind, etc.)
        # rather than assuming the same root cause without re-checking.
        if event_type == 'message.sent':
            logger.info(
                f"DIAG2 is_self_chat={adiyan_message['is_self_chat']} "
                f"from={data.get('from')!r} to={data.get('to')!r} "
                f"chatId={data.get('chatId')!r} author={data.get('author')!r} "
                f"kind={data.get('kind')!r}"
            )

        log_summary = f"{message_body[:50]}..." if message_body else f'[{media["kind"]}]' if media else '[no body]'
        logger.info(f"✅ Converted webhook from {contact_name}: {log_summary}")
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
        logger.info(" OpenWA Receiver initialized")

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
