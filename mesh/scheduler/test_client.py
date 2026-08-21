"""
Reusable manual test client for any agent under mesh/ - not scheduler-
specific, just pointed at scheduler by default. Sends one message, prints
the result. No pytest/fixtures - a quick way to hit a running agent by hand.

Run from the repo root (server must already be running separately):
    python -m mesh.scheduler.test_client "remind me every night to journal"
    python -m mesh.scheduler.test_client --url http://127.0.0.1:8420 "what jobs are scheduled?"
"""
import argparse
import asyncio

import httpx

from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.helpers import new_text_message
from a2a.types import Role, SendMessageRequest


async def send(url: str, text: str) -> None:
    # ClientConfig has no timeout field of its own - the real mechanism is
    # handing it a pre-configured httpx.AsyncClient. 120s, not httpx's 5s
    # default: a text message can chain up to 3 sequential LLM calls
    # (classify_skill -> extract_parameters -> resolve_schedule), and
    # qwen3:8b-16k's thinking mode alone takes ~10s per call.
    #
    # Kept open for the whole function, not closed after card resolution -
    # create_client's own send_message call reuses this same client, so
    # closing it early would break that, not just leave it wastefully open.
    async with httpx.AsyncClient(timeout=240) as httpx_client:
        resolver = A2ACardResolver(httpx_client=httpx_client, base_url=url)
        card = await resolver.get_agent_card()

        client = await create_client(
            agent=card,
            client_config=ClientConfig(streaming=False, httpx_client=httpx_client),
        )
        message = new_text_message(text, role=Role.ROLE_USER)
        request = SendMessageRequest(message=message)

        async for chunk in client.send_message(request):
            print(chunk)
        await client.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('text', help='Free-text message to send')
    parser.add_argument('--url', default='http://127.0.0.1:8420', help='Agent base URL')
    args = parser.parse_args()
    asyncio.run(send(args.url, args.text))
