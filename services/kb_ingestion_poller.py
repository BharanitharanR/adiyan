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
import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Awaitable, Callable, Dict, Optional

import httpx

import config.database as db
from core.memory_index import MemoryIndex
from services.openwa_service import OpenWAService, OpenWASessionNotFound
from services.owner_admin_handler import ADMIN_REPLY_TAG

logger = logging.getLogger('KBIngestionPoller')

MAX_BACKOFF_SECONDS = 60.0

PROCESSED_IDS_FILE = Path.home() / '.Adiyan' / 'processed_kb_message_ids.json'
PROCESSED_IDS_RETENTION_SECONDS = 7 * 24 * 60 * 60

PDF_MIMETYPE = 'application/pdf'


class KBIngestionPoller:
    """Polls the owner's self-chat for new messages: PDF documents get ingested into the
    KB, plain text gets routed to the admin handler (services/owner_admin_handler.py) if
    one is configured. One poller, one fetch per cycle, for both - not two independent
    pollers hitting OpenWA's rate-limited API (this project already hit that limit once
    from two pollers competing for budget; a third would make it worse, not better)."""

    def __init__(
        self,
        openwa_service: OpenWAService,
        memory_index: MemoryIndex,
        admin_handler=None,
        # PDF uploads are an occasional coach action, not a conversation - no reason to
        # poll at OpenWAPoller's 3s cadence. Both pollers share OpenWA's rate limit
        # budget (RATE_LIMIT_MAX_REQUESTS in penwa/.env), so a slower interval here
        # directly buys headroom for the client-facing poller instead of competing with it.
        poll_interval_seconds: float = 20.0,
        message_fetch_limit: int = 20,
        ignore_suffix: str = "**",
    ):
        self.openwa = openwa_service
        self.memory_index = memory_index
        self.admin_handler = admin_handler
        self.poll_interval_seconds = poll_interval_seconds
        self.message_fetch_limit = message_fetch_limit
        self.ignore_suffix = ignore_suffix

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
        try:
            self._owner_chat_id = await self._try_resolve_owner_chat_id()
        except Exception as e:
            logger.warning(f"⚠️  Could not resolve owner chat id at startup, will retry every poll cycle: {e}")
            self._owner_chat_id = None

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
        """A None return means the session genuinely has no phone yet (not linked -
        benign, expected during first-time setup, OpenWAService.get_own_chat_id()
        itself returns None for this case, no exception). A raised exception means
        the resolution call itself failed (rate limit, network, OpenWA down) - a
        real problem, not "waiting for setup" - so it's left to propagate to the
        caller's own error handling (start()'s try/except, or _poll_loop's existing
        backoff+dedup logging) instead of being silently swallowed here. Swallowing
        it used to hide real outages completely: a ~30 minute rate-limit stretch
        produced zero log output because every retry failed at logger.debug."""
        return await self.openwa.get_own_chat_id()

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

        body = (message.get('body') or '').strip()
        # A simple owner-side escape hatch: end a self-chat message with the configured
        # suffix (default **, changeable on the dashboard) to keep it out of admin
        # processing entirely - no job capture, no PDF ingestion, no admin agent call.
        # For jotting a private note to yourself in the same chat Adiyan otherwise
        # treats as commands. Guarded on a truthy suffix: str.endswith('') is always
        # True in Python, so an emptied-out setting must disable this, not swallow
        # every message.
        if self.ignore_suffix and body.endswith(self.ignore_suffix):
            return
        if ADMIN_REPLY_TAG in body:
            # This is a reply Adiyan itself sent into the self-chat (confirmation or
            # admin answer), not something the owner typed - self-chat messages are
            # always direction=outgoing regardless of which side sent them, so this
            # tag is the only way to tell them apart. Without this check, every reply
            # gets reprocessed as a new command on the next poll - a real runaway
            # self-conversation loop, confirmed live.
            return

        if message.get('type') != 'document':
            if body:
                # Is this answering an owner-'self' scheduled job (services/cron_scheduler.py)?
                # If so, capture it as job_data instead of treating it as a fresh admin
                # request - the client-facing equivalent of this check lives in
                # agents/validator_agent.py.
                from services.cron_scheduler import OWNER_PSEUDO_CONTACT
                pending = db.get_pending_job_response(OWNER_PSEUDO_CONTACT)
                if pending:
                    db.write_job_data(
                        pending['job_id'], key=f"response:{time.strftime('%Y-%m-%d')}",
                        value=body, contact_name=OWNER_PSEUDO_CONTACT,
                    )
                    db.clear_pending_job_response(pending['id'])
                    await self.openwa.send_message(
                        self._owner_chat_id, f"Got it, logged — thank you! 🙏\n\n{ADMIN_REPLY_TAG}",
                    )
                    return
                # Not a document, not a pending job response - route to the admin
                # handler if one's configured (skip reactions/stickers/other non-text noise).
                if self.admin_handler:
                    await self.admin_handler.handle_text_message(self._owner_chat_id, body)
            return

        # A self-chat message is always direction=outgoing (fromMe=true - you're both sender
        # and recipient), and OpenWA's media archive only archives INBOUND media ("the message
        # was sent BY this account" is explicitly excluded per its own docs) - so mediaMimetype/
        # mediaPath are always null here regardless of CHAT_MEDIA_ARCHIVE_ENABLED, and
        # download_media()'s archive-backed endpoint would always 404. The actual bytes are
        # already inline on the message record itself (metadata.media), unaffected by archiving.
        media_meta = (message.get('metadata') or {}).get('media') or {}
        if media_meta.get('mimetype') != PDF_MIMETYPE:
            return

        wa_message_id = message.get('waMessageId') or message_id
        filename = media_meta.get('filename') or f"document_{wa_message_id[-8:]}.pdf"
        media_b64 = media_meta.get('data')

        logger.info(f"📄 New PDF in owner self-chat: {filename}")
        try:
            if not media_b64:
                raise ValueError("Message has no inline media data to ingest")
            content = base64.b64decode(media_b64)
            chunk_count = self.memory_index.ingest_pdf(
                content=content,
                filename=filename,
                timestamp=time.strftime('%Y-%m-%dT%H:%M:%S'),
            )
            db.add_kb_document(filename, chunk_count, source='whatsapp_self_chat')
            await self.openwa.send_message(
                self._owner_chat_id,
                f"✅ Added '{filename}' to your knowledge base ({chunk_count} chunk(s)).\n\n{ADMIN_REPLY_TAG}",
            )
            logger.info(f"✅ Ingested '{filename}' into knowledge base ({chunk_count} chunks)")
        except Exception as e:
            logger.error(f"❌ Failed to ingest '{filename}': {e}", exc_info=True)
            try:
                await self.openwa.send_message(
                    self._owner_chat_id,
                    f"❌ Couldn't add '{filename}' to your knowledge base: {e}\n\n{ADMIN_REPLY_TAG}",
                )
            except Exception:
                pass  # best-effort - the ingestion failure is already logged above
