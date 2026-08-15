"""
Knowledge Base Ingestion Poller
Watches the business owner's own WhatsApp self-chat ("Message Yourself") for PDF
documents and ingests each one into Adiyan's knowledge base via Docling + the
memory index (core/memory_index.py).

Why the self-chat, and why a separate poller from OpenWAPoller: a message the
account owner sends to themselves is classified by WhatsApp Web as `fromMe: true`
-> `direction: outgoing`, which OpenWAPoller filters out entirely (it only reacts
to incoming client messages). The owner's self-chat id is also a sufficient
authorization check on its own - nobody but the account holder can ever put a
message into it - so this poller doesn't need any separate identity/whitelist
check the way the client-facing pipeline does.
"""
import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Awaitable, Callable, Dict, Optional

import httpx

from core.memory_index import MemoryIndex
from services.openwa_service import OpenWAService, OpenWASessionNotFound

logger = logging.getLogger('KBIngestionPoller')

MAX_BACKOFF_SECONDS = 60.0

PROCESSED_IDS_FILE = Path.home() / '.Adiyan' / 'processed_kb_message_ids.json'
PROCESSED_IDS_RETENTION_SECONDS = 7 * 24 * 60 * 60

PDF_MIMETYPE = 'application/pdf'


class KBIngestionPoller:
    """Polls the owner's self-chat for new PDF documents and ingests them into the KB."""

    def __init__(
        self,
        openwa_service: OpenWAService,
        memory_index: MemoryIndex,
        poll_interval_seconds: float = 5.0,
        message_fetch_limit: int = 20,
    ):
        self.openwa = openwa_service
        self.memory_index = memory_index
        self.poll_interval_seconds = poll_interval_seconds
        self.message_fetch_limit = message_fetch_limit

        self._processed_ids: Dict[str, int] = self._load_processed_ids()
        self._owner_chat_id: Optional[str] = None
        self._start_timestamp: Optional[int] = None
        self._running = False
        self._task: Optional[asyncio.Task] = None

        self._consecutive_failures = 0
        self._last_error_signature: Optional[str] = None

    @staticmethod
    def _load_processed_ids() -> Dict[str, int]:
        if not PROCESSED_IDS_FILE.exists():
            return {}
        try:
            with open(PROCESSED_IDS_FILE, 'r') as f:
                raw = json.load(f)
            cutoff = time.time() - PROCESSED_IDS_RETENTION_SECONDS
            return {mid: ts for mid, ts in raw.items() if ts >= cutoff}
        except Exception as e:
            logger.warning(f"Could not load KB dedup ledger, starting empty: {e}")
            return {}

    def _save_processed_ids(self):
        tmp_path = PROCESSED_IDS_FILE.with_suffix('.json.tmp')
        with open(tmp_path, 'w') as f:
            json.dump(self._processed_ids, f)
        os.replace(tmp_path, PROCESSED_IDS_FILE)

    async def start(self):
        if self._running:
            logger.warning("KB ingestion poller already running")
            return

        # The owner's chat id needs the session's phone, which isn't set until the
        # WhatsApp session is actually linked (QR scanned) - unlike the main poller,
        # failing to resolve this at startup isn't fatal, since a fresh install won't
        # have a linked phone yet. The poll loop retries resolution every cycle instead.
        self._owner_chat_id = await self._try_resolve_owner_chat_id()

        self._start_timestamp = int(time.time())
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(
            f"🚀 KB ingestion poller started (interval={self.poll_interval_seconds}s, "
            f"owner_chat_id={self._owner_chat_id or 'not yet linked'})"
        )

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("🛑 KB ingestion poller stopped")

    async def _try_resolve_owner_chat_id(self) -> Optional[str]:
        try:
            return await self.openwa.get_own_chat_id()
        except Exception as e:
            logger.debug(f"Could not resolve owner chat id yet: {e}")
            return None

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
                        "OpenWA rate-limited the KB poller (429) — backing off", is_429=True
                    )
                else:
                    sleep_seconds = self._on_poll_failure(f"KB poll cycle failed: {e}")
            except Exception as e:
                sleep_seconds = self._on_poll_failure(f"KB poll cycle failed: {e}", exc_info=True)

            await asyncio.sleep(sleep_seconds)

    def _on_poll_success(self):
        if self._consecutive_failures:
            logger.info(f"✅ KB poll cycle recovered after {self._consecutive_failures} failed attempt(s)")
        self._consecutive_failures = 0
        self._last_error_signature = None

    def _on_poll_failure(self, message: str, is_429: bool = False, exc_info: bool = False) -> float:
        self._consecutive_failures += 1
        if message != self._last_error_signature:
            logger.error(f"❌ {message}", exc_info=exc_info)
            self._last_error_signature = message
        else:
            logger.debug(f"KB poll cycle still failing ({self._consecutive_failures}x): {message}")

        base_interval = self.poll_interval_seconds * (3.0 if is_429 else 1.0)
        backoff = min(MAX_BACKOFF_SECONDS, base_interval * (2 ** min(self._consecutive_failures - 1, 6)))
        return backoff

    async def _poll_once(self):
        if not self._owner_chat_id:
            self._owner_chat_id = await self._try_resolve_owner_chat_id()
            if not self._owner_chat_id:
                return  # session still not linked - nothing to poll yet

        messages = await self.openwa.get_messages(self._owner_chat_id, limit=self.message_fetch_limit)

        # Oldest-first so confirmation replies land in the order the PDFs were sent.
        for message in reversed(messages):
            await self._handle_message(message)

    async def _handle_message(self, message: dict):
        message_id = message.get('id') or message.get('waMessageId')
        if not message_id or message_id in self._processed_ids:
            return

        timestamp = message.get('timestamp') or 0
        if timestamp < self._start_timestamp:
            return

        # Recorded before ingestion, not after: a crash mid-ingest should not re-run
        # (and re-embed, re-reply) the same PDF on restart.
        self._processed_ids[message_id] = int(time.time())
        self._save_processed_ids()

        if message.get('type') != 'document' or message.get('mediaMimetype') != PDF_MIMETYPE:
            return  # not a PDF - only PDFs are supported for now, everything else is ignored

        wa_message_id = message.get('waMessageId') or message_id
        filename = (message.get('metadata') or {}).get('filename') or f"document_{wa_message_id[-8:]}.pdf"

        logger.info(f"📄 New PDF in owner self-chat: {filename}")
        try:
            media = await self.openwa.download_media(self._owner_chat_id, wa_message_id)
            chunk_count = self.memory_index.ingest_pdf(
                content=media['content'],
                filename=filename,
                timestamp=time.strftime('%Y-%m-%dT%H:%M:%S'),
            )
            await self.openwa.send_message(
                self._owner_chat_id,
                f"✅ Added '{filename}' to your knowledge base ({chunk_count} chunk(s)).",
            )
            logger.info(f"✅ Ingested '{filename}' into knowledge base ({chunk_count} chunks)")
        except Exception as e:
            logger.error(f"❌ Failed to ingest '{filename}': {e}", exc_info=True)
            try:
                await self.openwa.send_message(
                    self._owner_chat_id,
                    f"❌ Couldn't add '{filename}' to your knowledge base: {e}",
                )
            except Exception:
                pass  # best-effort - the ingestion failure is already logged above
