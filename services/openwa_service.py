"""
OpenWA HTTP Service
Thin async client over OpenWA's REST API: resolves the session UUID by name,
lists chats, fetches messages, and sends replies.
"""

import logging
import os
from typing import Any, Dict, List, Optional

import httpx

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
        # A secret should not have to live in the config file to be used — env var wins if set.
        self.api_key = os.environ.get('OPENWA_API_KEY', api_key)
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
        """Send a text message to a chat. Returns OpenWA's response ({'messageId', 'timestamp'})."""
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
