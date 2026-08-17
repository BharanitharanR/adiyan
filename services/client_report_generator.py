"""
Nightly per-client "personality report" digest - a PDF summarizing each active
client's recent journal/job-data activity and engagement, sent to the owner's
own WhatsApp chat.

Structurally different from a normal cron job (services/cron_scheduler.py's
_compose_message): a standard job composes ONE message and sends it to a
resolved set of targets. This produces N separate reports, one per active
client, all delivered to the SAME recipient (the owner) - so it bypasses the
LLM tool-calling composer entirely in favor of one fixed, deterministic report
per client: gather -> one narrative-writing LLM call -> render to PDF -> send.
Sending is still never an LLM decision, same principle as every other job.

A single client's failure (bad data, a slow model call, an OpenWA hiccup) must
never take down the rest of the batch - each client is wrapped independently,
mirroring the lesson from propose_new_routine's own nested-timeout bug.
"""
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage

import config.database as db
from services.openwa_service import OpenWAService
from services.owner_admin_handler import ADMIN_REPLY_TAG
from services.pdf_service import generate_pdf_from_markdown

logger = logging.getLogger('ClientReportGenerator')

REPORT_TIMEOUT_SECONDS = 60
JOURNAL_ENTRY_LIMIT = 30

REPORT_SYSTEM_PROMPT = (
    "You write a short, grounded personality/engagement report about ONE client of a "
    "WhatsApp coaching service, based only on their own journal entries and activity "
    "provided below - never invent facts not supported by the material. Respond in markdown "
    "with exactly these ## headers, in order:\n"
    "## Overview\nOne or two sentences on who they are and how they're engaging.\n"
    "## Strengths\n2-3 bullet points (using '- ').\n"
    "## Areas to watch\n1-2 bullet points, framed constructively, not judgmentally.\n"
    "## Notable recent entries\n1-2 short direct quotes or paraphrases from their own journal, if any exist.\n"
    "## Suggested next step\nOne concrete, specific thing the owner could do or send this client.\n"
    "If there isn't enough material for a section, write 'Not enough data yet.' under that header instead of "
    "guessing. Keep the whole report under 250 words. No text before the first header."
)


async def _compose_personality_report(client: Dict[str, Any], journal_entries: List[Dict[str, Any]],
                                       model_name: str, ollama_url: str) -> str:
    from langchain_ollama import ChatOllama
    import asyncio

    lines = [f"Client: {client['contact_name']}"]
    if not journal_entries:
        lines.append("(No journal or job-data entries recorded yet.)")
    for entry in journal_entries:
        lines.append(f"[{entry['created_at']}] ({entry['job_name']}): {entry['value']}")

    model = ChatOllama(model=model_name, base_url=ollama_url, temperature=0.4)
    result = await asyncio.wait_for(
        model.ainvoke([
            SystemMessage(content=REPORT_SYSTEM_PROMPT),
            HumanMessage(content="\n".join(lines)),
        ]),
        timeout=REPORT_TIMEOUT_SECONDS,
    )

    from core.token_usage import record as record_token_usage
    record_token_usage(
        context_type='client_report', model=model_name, messages=[result],
        context_label=client['contact_name'], contact_name=client['contact_name'],
    )
    return (result.content or '').strip()


async def run_client_reports_digest(openwa: OpenWAService, owner_chat_id: Optional[str],
                                     model_name: str, ollama_url: str) -> Dict[str, Any]:
    """One PDF per active client, all sent to the owner. Returns a summary dict
    - never raises for a single client's failure, only for something that makes
    the whole run pointless (no owner_chat_id to send to at all)."""
    if not owner_chat_id:
        logger.warning("⚠️  Client reports digest: no owner chat id resolved, nothing sent")
        return {'sent': 0, 'failed': 0, 'skipped': 0}

    clients = db.list_clients(active_only=True)
    sent, failed = 0, 0
    for client in clients:
        try:
            entries = db.read_job_data_for_contact(client['contact_name'], limit=JOURNAL_ENTRY_LIMIT)
            report_markdown = await _compose_personality_report(client, entries, model_name, ollama_url)
            pdf_bytes = generate_pdf_from_markdown(
                report_markdown, title=f"Personality Report: {client['contact_name']}",
            )
            filename = f"{client['contact_name'].replace(' ', '_')}_report_{datetime.now().strftime('%Y%m%d')}.pdf"
            # OpenWA persists a sent document's caption back into the message's
            # `body` field (penwa's message.service.js sendDocument: body =
            # caption || filename) - which is exactly the field
            # kb_ingestion_poller.py checks for ADMIN_REPLY_TAG before its
            # self-chat routing. Without the tag here, this PDF looks
            # indistinguishable from the owner uploading a document to ingest -
            # it would loop back on the next poll and get silently added to the
            # knowledge base, the document-message equivalent of the "job
            # answering its own prompt" bug fixed earlier for text messages.
            await openwa.send_document(
                owner_chat_id, filename=filename, data=pdf_bytes,
                caption=f"Personality report: {client['contact_name']}\n\n{ADMIN_REPLY_TAG}",
            )
            sent += 1
        except Exception as e:
            failed += 1
            logger.error(f"❌ Client report failed for '{client['contact_name']}': {e}", exc_info=True)

    logger.info(f"✅ Client reports digest: {sent} sent, {failed} failed, {len(clients)} total active clients")
    return {'sent': sent, 'failed': failed, 'skipped': 0}
