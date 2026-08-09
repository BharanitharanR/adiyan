"""
OpenWA Message Poller
Replaces OpenWA webhooks (blocked for localhost destinations) with polling.
Fetches recent messages on an interval, filters to new incoming ones, and
feeds each through the orchestrator via a callback.
"""

import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional, Set

from services.openwa_receiver import OpenWAAdapter
from services.openwa_service import OpenWAService, OpenWASessionNotFound

logger = logging.getLogger('OpenWAPoller')


class OpenWAPoller:
    """Polls OpenWA for new incoming messages and dispatches them to the orchestrator."""

    def __init__(
        self,
        openwa_service: OpenWAService,
        orchestrator_callback: Callable[[dict], Awaitable[dict]],
        poll_interval_seconds: float = 3.0,
        message_fetch_limit: int = 50,
    ):
        self.openwa = openwa_service
        self.orchestrator_callback = orchestrator_callback
        self.poll_interval_seconds = poll_interval_seconds
        self.message_fetch_limit = message_fetch_limit

        # Messages already dispatched — keyed by OpenWA's own message id, not a single
        # "last seen" pointer, since more than one new message can appear between polls.
        self._processed_ids: Set[str] = set()
        self._start_timestamp: Optional[int] = None
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self):
        """Resolve the session, mark the current time as the replay cutoff, and start polling."""
        if self._running:
            logger.warning("Poller already running")
            return

        try:
            session_id = await self.openwa.resolve_session_id()
            logger.info(f"✅ Resolved OpenWA session id: {session_id}")
        except OpenWASessionNotFound as e:
            logger.error(f"❌ Cannot start poller: {e}")
            raise

        # Never replay history from before the poller started — same fix the POC needed.
        self._start_timestamp = int(time.time())

        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(
            f"🚀 OpenWA poller started (interval={self.poll_interval_seconds}s, "
            f"cutoff_timestamp={self._start_timestamp})"
        )

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("🛑 OpenWA poller stopped")

    async def _poll_loop(self):
        while self._running:
            try:
                await self._poll_once()
            except OpenWASessionNotFound:
                logger.error("OpenWA session disappeared — will retry resolution next cycle")
            except Exception as e:
                logger.error(f"❌ Poll cycle failed: {e}", exc_info=True)

            await asyncio.sleep(self.poll_interval_seconds)

    async def _poll_once(self):
        messages = await self.openwa.get_all_recent_messages(limit=self.message_fetch_limit)

        # Oldest-first so replies are sent in the order messages actually arrived.
        for message in reversed(messages):
            await self._handle_message(message)

    async def _handle_message(self, message: dict):
        message_id = message.get('id') or message.get('waMessageId')
        if not message_id or message_id in self._processed_ids:
            return

        if message.get('direction') != 'incoming':
            return

        timestamp = message.get('timestamp') or 0
        if timestamp < self._start_timestamp:
            return

        self._processed_ids.add(message_id)

        adiyan_message = OpenWAAdapter.polled_message_to_adiyan(
            message, session_name=self.openwa.session_name
        )
        if not adiyan_message:
            return

        logger.info(
            f"📨 New message from {adiyan_message['contact_name']}: "
            f"{adiyan_message['message_body'][:80]}"
        )

        try:
            await self.orchestrator_callback(adiyan_message)
            logger.info(f"✅ Processed message for {adiyan_message['contact_name']}")
        except Exception as e:
            logger.error(f"❌ Orchestrator failed for {adiyan_message['contact_name']}: {e}", exc_info=True)
