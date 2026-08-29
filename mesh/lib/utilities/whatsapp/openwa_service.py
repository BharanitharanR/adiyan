"""
OpenWA HTTP Service
Thin async client over OpenWA's REST API: resolves the session UUID by name,
lists chats, fetches messages, and sends replies.
"""

import logging
import os
from typing import Any, Dict, List, Optional

import httpx

from mesh.lib.utilities import watermark

logger = logging.getLogger('OpenWAService')


class OpenWASessionNotFound(Exception):
    """Raised when no OpenWA session matches the configured session name."""


class OpenWAService:
    """Async wrapper around OpenWA's REST API for a single named session.

    This instance is shared across at least two independent asyncio event loops -
    the OpenWA poller's own long-lived loop, and a fresh loop asyncio.run() spins
    up per message on the RabbitMQ consumer thread. A persistent httpx.AsyncClient
    would bind its internal connection-pool lock to whichever loop touched it
    first, then error ("bound to a different event loop") the first time a call
    came from the other one. Opening a short-lived client per call sidesteps this
    entirely - it's created and closed within a single coroutine's lifetime, on
    whichever loop happens to be running, so it never outlives or crosses loops.
    On localhost this costs a cheap loopback TCP setup per call, not real latency.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        session_name: str,
        request_timeout: float = 10.0,
    ):
        self.base_url = base_url.rstrip('/')
        # Priority: OS Keychain vault (mesh/lib/secrets_vault.py) > env var > the
        # api_key passed in (which itself already reflects the vault via
        # control_plane's own resolution - this is a defensive second check for
        # any caller that constructs OpenWAService directly, e.g. Tests/).
        from mesh.lib.secrets_vault import get_secret
        self.api_key = get_secret('OPENWA_API_KEY') or os.environ.get('OPENWA_API_KEY', api_key)
        self.session_name = session_name
        self.request_timeout = request_timeout
        self._session_id: Optional[str] = None

        logger.info(f"OpenWAService initialized for session '{session_name}' at {self.base_url}")

    def _new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                'X-API-Key': self.api_key,
                'Content-Type': 'application/json',
            },
            timeout=self.request_timeout,
        )

    async def close(self):
        """No-op: there's no persistent client to close - kept so existing
        shutdown code calling this doesn't need to change."""
        pass

    async def resolve_session_id(self, force_refresh: bool = False) -> str:
        """
        Resolve the OpenWA session UUID for `self.session_name`.

        The UUID is not stable across session recreation, but the name is — this is
        looked up via the documented `GET /api/sessions` endpoint (never OpenWA's
        internal files) and cached until a caller asks for a forced refresh.
        """
        if self._session_id and not force_refresh:
            return self._session_id

        async with self._new_client() as client:
            response = await client.get('/api/sessions')
        response.raise_for_status()
        sessions = response.json()

        for session in sessions:
            if session.get('name') == self.session_name:
                self._session_id = session['id']
                status = session.get('status')
                if status != 'ready':
                    logger.warning(
                        f"OpenWA session '{self.session_name}' resolved but status is '{status}', not 'ready'"
                    )
                return self._session_id

        raise OpenWASessionNotFound(
            f"No OpenWA session named '{self.session_name}' found. "
            f"Available: {[s.get('name') for s in sessions]}"
        )

    async def _session_id_or_refresh(self) -> str:
        try:
            return await self.resolve_session_id()
        except OpenWASessionNotFound:
            raise

    async def get_chats(self) -> List[Dict[str, Any]]:
        """Return all chats for the resolved session."""
        session_id = await self._session_id_or_refresh()
        async with self._new_client() as client:
            response = await client.get(f'/api/sessions/{session_id}/chats')
        response.raise_for_status()
        return response.json()

    async def get_messages(self, chat_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Return recent messages for a specific chat, most-recent-first (per OpenWA's ordering)."""
        session_id = await self._session_id_or_refresh()
        async with self._new_client() as client:
            response = await client.get(
                f'/api/sessions/{session_id}/messages',
                params={'chatId': chat_id, 'limit': limit},
            )
        response.raise_for_status()
        data = response.json()
        return data.get('messages', data if isinstance(data, list) else [])

    async def get_all_recent_messages(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Return recent messages across the whole session (no chatId filter)."""
        session_id = await self._session_id_or_refresh()
        async with self._new_client() as client:
            response = await client.get(
                f'/api/sessions/{session_id}/messages',
                params={'limit': limit},
            )
        response.raise_for_status()
        data = response.json()
        return data.get('messages', data if isinstance(data, list) else [])

    async def send_message(self, chat_id: str, text: str) -> Dict[str, Any]:
        """Send a text message to a chat. Returns OpenWA's response ({'messageId', 'timestamp'}).

        Strict rule: every outgoing message carries the watermark
        (services/watermark.py) - applied here, not in any individual
        caller, because this is the one method every send path (mesh/'s
        WhatsApp MCP send_message tool, Scheduler's run_routine calling
        this directly, the legacy pipeline) ultimately goes through. No
        caller can opt out."""
        text = await watermark.apply(text)
        session_id = await self._session_id_or_refresh()
        async with self._new_client() as client:
            response = await client.post(
                f'/api/sessions/{session_id}/messages/send-text',
                json={'chatId': chat_id, 'text': text},
            )
        response.raise_for_status()
        result = response.json()
        logger.info(f"Sent message to {chat_id}: {result.get('messageId')}")
        return result

    async def send_document(self, chat_id: str, filename: str, data: bytes,
                             mimetype: str = 'application/pdf', caption: Optional[str] = None) -> Dict[str, Any]:
        """Send a file (e.g. a generated PDF report) to a chat, base64-encoded
        over OpenWA's send-document endpoint. Own client timeout, not the default
        10s send_message uses - a report-sized PDF, base64-inflated ~33%, is a
        meaningfully bigger payload than a text message on the same local hop."""
        import base64
        session_id = await self._session_id_or_refresh()
        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers={'X-API-Key': self.api_key, 'Content-Type': 'application/json'},
            timeout=60.0,
        ) as client:
            response = await client.post(
                f'/api/sessions/{session_id}/messages/send-document',
                json={
                    'chatId': chat_id,
                    'base64': base64.b64encode(data).decode('ascii'),
                    'mimetype': mimetype,
                    'filename': filename,
                    **({'caption': caption} if caption else {}),
                },
            )
        response.raise_for_status()
        result = response.json()
        logger.info(f"Sent document '{filename}' to {chat_id}: {result.get('messageId')}")
        return result

    async def send_image(self, chat_id: str, filename: str, data: bytes,
                          mimetype: str = 'image/png', caption: Optional[str] = None) -> Dict[str, Any]:
        """Send a real image message (renders as a photo in the chat, not a
        file attachment) - OpenWA's own send-image endpoint, distinct from
        send-document. Confirmed live: send_document() with an image
        mimetype hung until httpx.ReadTimeout every single time, even
        against the owner's own self-chat with an 8.8KB PNG - penwa's own
        message.service.ts handles 'image' and 'document' as genuinely
        different WhatsApp message types (different chatId save path,
        different underlying engine call), not just a label on the same
        document-send flow. Same payload shape as send_document() otherwise,
        just a different endpoint and default mimetype."""
        import base64
        session_id = await self._session_id_or_refresh()
        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers={'X-API-Key': self.api_key, 'Content-Type': 'application/json'},
            timeout=60.0,
        ) as client:
            response = await client.post(
                f'/api/sessions/{session_id}/messages/send-image',
                json={
                    'chatId': chat_id,
                    'base64': base64.b64encode(data).decode('ascii'),
                    'mimetype': mimetype,
                    'filename': filename,
                    **({'caption': caption} if caption else {}),
                },
            )
        response.raise_for_status()
        result = response.json()
        logger.info(f"Sent image '{filename}' to {chat_id}: {result.get('messageId')}")
        return result

    async def send_voice(self, chat_id: str, data: bytes, mimetype: str = 'audio/ogg; codecs=opus') -> Dict[str, Any]:
        """Send audio as a real WhatsApp voice note (PTT - mic bubble +
        waveform, not a plain audio-file attachment) - ptt=True is what
        selects that rendering on OpenWA's send-audio endpoint. Callers
        should hand this Opus-encoded OGG bytes (AdiyanReader's own
        tts.py transcodes Orpheus's raw WAV output before calling this) -
        OpenWA's own docs note plain WAV/PCM is not reliable for the PTT
        waveform UI to render correctly, only audio/ogg;codecs=opus is.

        No watermark applied, same as send_document() - there is no text
        to append a text signature to, and OpenWA's send-audio endpoint
        has no caption field to carry one in either."""
        import base64
        session_id = await self._session_id_or_refresh()
        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers={'X-API-Key': self.api_key, 'Content-Type': 'application/json'},
            timeout=60.0,
        ) as client:
            response = await client.post(
                f'/api/sessions/{session_id}/messages/send-audio',
                json={
                    'chatId': chat_id,
                    'base64': base64.b64encode(data).decode('ascii'),
                    'mimetype': mimetype,
                    'ptt': True,
                },
            )
        response.raise_for_status()
        result = response.json()
        logger.info(f"Sent voice note to {chat_id}: {result.get('messageId')}")
        return result

    async def get_session_status(self) -> Dict[str, Any]:
        """Return the raw session record (status, phone, pushName, etc.)."""
        session_id = await self._session_id_or_refresh()
        async with self._new_client() as client:
            response = await client.get(f'/api/sessions/{session_id}')
        response.raise_for_status()
        return response.json()

    async def is_connected(self) -> bool:
        try:
            status = await self.get_session_status()
            return status.get('status') == 'ready'
        except Exception as e:
            logger.warning(f"Could not check OpenWA session status: {e}")
            return False

    async def get_own_chat_id(self) -> Optional[str]:
        """Return the linked account's own chat id (its self-chat / 'Message Yourself' JID),
        or None if the session has no phone yet (not linked / still initializing).

        This is the business owner's identity for KB ingestion (services/kb_ingestion_poller.py):
        nobody but the account holder can ever put a message into their own self-chat, so a
        message's chatId matching this is a sufficient authorization check on its own.
        """
        status = await self.get_session_status()
        phone = status.get('phone')
        if not phone:
            return None
        return await self.resolve_chat_id(phone)

    async def resolve_chat_id(self, phone: str) -> Optional[str]:
        """Resolve any phone number to its WhatsApp chat id, or None if OpenWA can't
        find a match. NOT simply f'{phone}@c.us' - some accounts (confirmed against a
        real linked session) address individual chats via WhatsApp's newer @lid scheme
        instead of phone-based @c.us, with a lid number that bears no relation to the
        phone number. The contacts/check endpoint resolves to whichever JID form the
        account actually uses.

        Needed beyond get_own_chat_id() (which only resolves the owner's own number)
        because a client's own `lid` is only ever captured live off an incoming
        message (agents/parser_agent.py's registration flow) - a client registered
        before that capture existed, or added by the owner via the admin channel's
        add_client with only a phone number, has no lid on file at all. Proactive
        sends (services/cron_scheduler.py) can't rely on a live message to supply
        one, so they need to resolve it from the phone number instead.

        `phone` is not always a real phone number, despite the parameter name -
        confirmed live: a contact whose real number was never captured (the lid
        was the only identifier ever seen for them) gets that lid stored as their
        "phone_number" wherever a caller needed something to key on, and it's
        already a valid WhatsApp chat id in its own right - stripping it to
        digits and running it through the phone-to-JID lookup below fails
        outright, since the digits inside a lid aren't a dialable number at all
        (mesh/adiyan_reader/skills/read_next_page.py's own reading_jobs.phone_number
        column hit exactly this - a registered client's book reading silently
        never sent because of it). Anything that already looks like a JID
        (carries an '@') is returned as-is, no lookup needed.

        Strips non-digit characters first - confirmed live that the endpoint 500s
        on a leading '+' (a stored phone number like '+919080089081' must become
        '919080089081'), while session status's own `phone` field (used by
        get_own_chat_id) never has one to begin with.
        """
        if '@' in phone:
            return phone
        digits_only = ''.join(ch for ch in phone if ch.isdigit())
        session_id = await self._session_id_or_refresh()
        async with self._new_client() as client:
            response = await client.get(f'/api/sessions/{session_id}/contacts/check/{digits_only}')
        response.raise_for_status()
        return response.json().get('whatsappId')

    async def download_media(self, chat_id: str, message_id: str) -> Dict[str, Any]:
        """Download a message's archived media (requires CHAT_MEDIA_ARCHIVE_ENABLED=true in
        OpenWA). Returns {'content': bytes, 'mimetype': str}."""
        session_id = await self._session_id_or_refresh()
        async with self._new_client() as client:
            response = await client.get(f'/api/sessions/{session_id}/messages/{chat_id}/{message_id}/media')
        response.raise_for_status()
        return {
            'content': response.content,
            'mimetype': response.headers.get('content-type', 'application/octet-stream'),
        }
