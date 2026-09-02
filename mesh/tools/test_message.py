#!/usr/bin/env python3
"""
Sends a synthetic message straight into Orchestrator's handle_message
skill, exactly the same call whatsapp_mcp's webhook makes (mesh/mcp/
whatsapp/server.py) - but from a script, not a real WhatsApp message
routed through OpenWA + ngrok. Lets you test the full routing ->
classify -> humanize -> ask() -> Inference Router chain locally, without
needing a phone, a tunnel, or another machine.

Needs Orchestrator (and whatever it might route to - scheduler, memory,
analysis, inference_router, ollama) actually running:
    mesh/start_all.sh start orchestrator scheduler memory analysis inference_router
(or the whole mesh, if you want the real thing end to end)

Run from the repo root:
    python3 -m mesh.tools.test_message "are there any scheduled jobs?"
    python3 -m mesh.tools.test_message "what is 2+2 communitySearch" --chat-id test_chat_1

Does NOT actually send anything over WhatsApp - handle_message.py's own
delivery step still fires for real (a real send_message call to
whatsapp_mcp), so a real reply DOES go out to whatever chat_id you give,
if that chat_id is registered. Use a chat_id nobody's watching, or your
own self-chat's real chat_id, to actually see the reply land.
"""
import argparse
import asyncio
from typing import Optional

from mesh.lib import permissions
from mesh.lib.a2a_client import call_agent
from mesh.lib.mcp_client import call_tool

ORCHESTRATOR_URL = 'http://127.0.0.1:8426'
WHATSAPP_MCP_URL = 'https://127.0.0.1:8425/mcp'  # self-signed HTTPS, matches mesh/orchestrator/constants.py


async def _resolve_own_phone() -> Optional[str]:
    """--self-chat alone isn't enough to pass rules_engine.check()'s gate -
    it only matters INSIDE the is_owner(from_number) branch, and is_owner()
    needs a real from_number that matches whatsapp_mcp's own get_own_phone
    to ever return True. Resolved here the same way rules_engine.is_owner()
    itself does, so this script's --self-chat flag actually means what it
    says instead of silently falling through to the unregistered-stranger
    path (confirmed live: this is exactly what happened before this existed
    - reply=None, delivered=False, no error, no log line, indistinguishable
    from an actual bug)."""
    token = permissions.mint_token('orchestrator', 'service')
    try:
        result = await call_tool(WHATSAPP_MCP_URL, 'get_own_phone', {}, token=token)
        return result.get('phone')
    except Exception as e:
        print(f'Could not resolve own phone via whatsapp_mcp ({e}) - is it running? Falling back to no from_number.')
        return None


async def main(text: str, chat_id: str, contact_name: str, is_self_chat: bool) -> None:
    from_number = await _resolve_own_phone() if is_self_chat else None
    # An owner-authored message only ever triggers Adiyan when explicitly
    # @-mentioned (rules_engine.check()'s own eligibility gate) - without
    # this, --self-chat still silently returns (None, None), same as a
    # missing from_number does.
    if is_self_chat and '@adiyan' not in text.lower():
        text = f'@Adiyan {text}'
    print(f'Sending to Orchestrator: chat_id={chat_id!r} text={text!r} is_self_chat={is_self_chat} from_number={from_number!r}')
    try:
        result = await call_agent(ORCHESTRATOR_URL, 'handle_message', {
            'text': text,
            'chat_id': chat_id,
            'contact_name': contact_name,
            'from_number': from_number,
            'image': None,
            'document': None,
            'is_self_chat': is_self_chat,
        })
    except Exception as e:
        print(f'\nFAILED: {e}')
        return
    print(f"\nreply:     {result.get('reply')!r}")
    print(f"delivered: {result.get('delivered')}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('text', help='The message text to send')
    parser.add_argument('--chat-id', default='test_chat_local', help='chat_id to route the reply to (default: a fake one nobody sees)')
    parser.add_argument('--contact-name', default='Test User')
    parser.add_argument('--self-chat', action='store_true', help='Simulate the owner\'s own self-chat (skips the registered-client gate)')
    args = parser.parse_args()
    asyncio.run(main(args.text, args.chat_id, args.contact_name, args.self_chat))
