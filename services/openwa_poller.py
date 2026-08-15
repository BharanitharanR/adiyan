"""
OpenWA Message Poller
Replaces OpenWA webhooks (blocked for localhost destinations) with polling.
Fetches recent messages on an interval, filters to new incoming ones, and
feeds each through the orchestrator via a callback.
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Awaitable, Callable, Dict, Optional

import httpx

from services.openwa_receiver import OpenWAAdapter
from services.openwa_service import OpenWAService, OpenWASessionNotFound

logger = logging.getLogger('OpenWAPoller')

# Cap how long a failing poll cycle will back off to — even a persistent outage
# should still retry occasionally, not go silent forever.
MAX_BACKOFF_SECONDS = 60.0

# WhatsApp's own chat-id convention: group chats always end in "@g.us";
# individual chats end in "@c.us" or "@lid". This is how we tell them apart -
# there's no separate "isGroup" flag on the message record itself.
GROUP_CHAT_SUFFIX = '@g.us'

# Dedup ledger persisted across restarts — an in-memory-only set would forget
# every message it had already replied to the moment the process restarted,
# which is exactly how a restart mid-flight causes a double send.
PROCESSED_IDS_FILE = Path.home() / '.Adiyan' / 'processed_message_ids.json'

# How long a processed-id entry is kept before it's pruned from the ledger.
# Bounds the file's growth; far longer than any realistic restart gap.
PROCESSED_IDS_RETENTION_SECONDS = 7 * 24 * 60 * 60


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
        # Persisted to disk (message_id -> timestamp) so a process restart can't forget
        # a message it already replied to and send a second reply for it.
        self._processed_ids: Dict[str, int] = self._load_processed_ids()
        self._start_timestamp: Optional[int] = None
        self._running = False
        self._task: Optional[asyncio.Task] = None

        # Backoff state for a failing poll cycle — avoids hammering an already-
        # throttled or down endpoint, and avoids re-logging the same failure
        # every cycle (only the first occurrence and the eventual recovery earn a log).
        self._consecutive_failures = 0
        self._last_error_signature: Optional[str] = None

    @staticmethod
    def _load_processed_ids() -> Dict[str, int]:
        """Load the persisted dedup ledger, pruning entries old enough that a
        restart can't possibly still be racing against them."""
        if not PROCESSED_IDS_FILE.exists():
            return {}
        try:
            with open(PROCESSED_IDS_FILE, 'r') as f:
                raw = json.load(f)
            cutoff = time.time() - PROCESSED_IDS_RETENTION_SECONDS
            return {mid: ts for mid, ts in raw.items() if ts >= cutoff}
        except Exception as e:
            logger.warning(f"Could not load processed-message ledger, starting empty: {e}")
            return {}

    def _save_processed_ids(self):
        tmp_path = PROCESSED_IDS_FILE.with_suffix('.json.tmp')
        with open(tmp_path, 'w') as f:
            json.dump(self._processed_ids, f)
        os.replace(tmp_path, PROCESSED_IDS_FILE)

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
                self._on_poll_success()
                sleep_seconds = self.poll_interval_seconds
            except OpenWASessionNotFound:
                sleep_seconds = self._on_poll_failure(
                    "OpenWA session disappeared — will retry resolution next cycle"
                )
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    sleep_seconds = self._on_poll_failure(
                        "OpenWA rate-limited the poller (429) — backing off", is_429=True
                    )
                else:
                    sleep_seconds = self._on_poll_failure(f"Poll cycle failed: {e}")
            except Exception as e:
                sleep_seconds = self._on_poll_failure(f"Poll cycle failed: {e}", exc_info=True)

            await asyncio.sleep(sleep_seconds)

    def _on_poll_success(self):
        if self._consecutive_failures:
            logger.info(f"✅ Poll cycle recovered after {self._consecutive_failures} failed attempt(s)")
        self._consecutive_failures = 0
        self._last_error_signature = None

    def _on_poll_failure(self, message: str, is_429: bool = False, exc_info: bool = False) -> float:
        self._consecutive_failures += 1

        # Only log the first occurrence of a given failure — repeats of the same
        # error every cycle are noise, not new information.
        if message != self._last_error_signature:
            logger.error(f"❌ {message}", exc_info=exc_info)
            self._last_error_signature = message
        else:
            logger.debug(f"Poll cycle still failing ({self._consecutive_failures}x): {message}")

        base_interval = self.poll_interval_seconds * (3.0 if is_429 else 1.0)
        backoff = min(MAX_BACKOFF_SECONDS, base_interval * (2 ** min(self._consecutive_failures - 1, 6)))
        return backoff

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

        # Recorded (and persisted) before dispatch, not after: if the process dies
        # mid-reply, we'd rather risk a missed reply than a duplicate one on restart.
        self._processed_ids[message_id] = int(time.time())
        self._save_processed_ids()

        chat_id = message.get('chatId') or ''
        if chat_id.endswith(GROUP_CHAT_SUFFIX):
            logger.info(
                f"⏭️  Skipping group message in {chat_id} — Adiyan only responds in 1:1 chats"
            )
            return

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
