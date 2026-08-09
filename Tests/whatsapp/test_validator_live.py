#!/usr/bin/env python3
"""
Live test: OpenWAPoller -> ParserAgent -> ValidatorAgent (whitelist only).
No RabbitMQ, no Ollama, no Qdrant, no main.py needed.

Sends a reply on OpenWA confirming the whitelist decision, so you can watch
it happen live in WhatsApp.
"""

import asyncio
import logging
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.base_agent import AgentState
from agents.parser_agent import ParserAgent
from agents.validator_agent import ValidatorAgent
from services.openwa_service import OpenWAService
from services.openwa_poller import OpenWAPoller

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(message)s')
logging.getLogger('httpx').setLevel(logging.WARNING)  # quiet the per-poll HTTP noise

OPENWA_URL = 'http://localhost:2785'
OPENWA_API_KEY = ''  # set OPENWA_API_KEY env var - OpenWAService reads it over this default
SESSION_NAME = 'executive-coach'


async def main():
    openwa = OpenWAService(base_url=OPENWA_URL, api_key=OPENWA_API_KEY, session_name=SESSION_NAME)
    parser = ParserAgent({})
    validator = ValidatorAgent({})

    async def process(message: dict):
        state = AgentState(
            message_id=message.get('message_id', str(uuid.uuid4())),
            contact_name=message['contact_name'],
            lid=message['lid'],
            message_body=message['message_body'],
        )

        state = await parser.execute(state)
        if state.error:
            print(f"[PARSER ERROR] {state.contact_name}: {state.error}")
            return

        state = await validator.execute(state)
        if state.error:
            print(f"[VALIDATOR ERROR] {state.contact_name}: {state.error}")
            return

        action = state.metadata.get('action')
        if action == 'registered':
            reply = f"✅ You're registered, {state.contact_name}! Whitelist status: {state.is_whitelisted}"
        elif action == 'unregistered':
            reply = f"✅ You're unregistered, {state.contact_name}. Whitelist status: {state.is_whitelisted}"
        elif state.is_whitelisted:
            reply = f"✅ [WHITELISTED] Message received: \"{state.message_body}\""
        else:
            reply = f"❌ [NOT WHITELISTED] Send \"register me\" to get whitelisted."

        print(f"\n>>> {state.contact_name}: is_whitelisted={state.is_whitelisted} action={action}\n")

        try:
            await openwa.send_message(state.lid, reply)
        except Exception as e:
            print(f"[SEND FAILED] {e}")

    poller = OpenWAPoller(
        openwa_service=openwa,
        orchestrator_callback=process,
        poll_interval_seconds=3.0,
    )

    await poller.start()
    print("=" * 70)
    print("Validator/whitelist live test running.")
    print("Send WhatsApp messages now:")
    print('  - "register me ..." to whitelist yourself')
    print('  - any other message to check whitelist status')
    print('  - "unregister me ..." to remove yourself')
    print("Press Ctrl+C to stop.")
    print("=" * 70)

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await poller.stop()
        await openwa.close()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
