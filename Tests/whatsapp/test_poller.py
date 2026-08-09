#!/usr/bin/env python3
"""Manual smoke test for OpenWAPoller against the live OpenWA instance."""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.openwa_service import OpenWAService
from services.openwa_poller import OpenWAPoller

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(message)s')


async def fake_orchestrator(message: dict) -> dict:
    print(f"\n>>> ORCHESTRATOR RECEIVED: {message}\n")
    return {'status': 'ok'}


async def main():
    service = OpenWAService(
        base_url='http://localhost:2785',
        api_key='',  # set OPENWA_API_KEY env var - OpenWAService reads it over this default
        session_name='executive-coach',
    )

    poller = OpenWAPoller(
        openwa_service=service,
        orchestrator_callback=fake_orchestrator,
        poll_interval_seconds=3.0,
    )

    await poller.start()
    print("Poller running for 20 seconds. Send a WhatsApp message now...")
    await asyncio.sleep(20)
    await poller.stop()
    await service.close()


if __name__ == '__main__':
    asyncio.run(main())
