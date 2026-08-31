"""
One call, for any agent that needs to tell the owner something over
WhatsApp - resolves who "owner" actually is, then sends through
whatsapp_mcp's gated send_message tool. Every caller still mints its own
token against its own tier (see mesh/lib/permissions_config.json's
config_server_otp/adiyan_reader_service for the pattern this expects new
agents to follow) - this helper doesn't grant anything by itself, it just
removes the boilerplate of doing the mint/resolve/send dance by hand.

Deliberately does NOT go through mesh.lib.utilities.whatsapp.openwa_service
directly - that bypasses whatsapp_mcp's enforce_mcp_permission() entirely
(see mesh/scheduler/skills/run_routine.py and mesh/adiyan_reader/skills/
read_next_page.py for two places that currently do exactly that, and why
it's a real gap, not a style choice). Every send through this helper is
checked against the caller's own tier, same as config_server/otp.py's
hand-written version of this same flow.
"""
import logging
from typing import Optional

from mesh.lib import permissions
from mesh.lib.mcp_client import call_tool

logger = logging.getLogger('NotifyOwner')

WHATSAPP_MCP_URL = 'https://127.0.0.1:8425/mcp'


def _digits_only(phone: Optional[str]) -> str:
    return ''.join(ch for ch in (phone or '') if ch.isdigit())


async def notify_owner(agent_id: str, tier: str, text: str) -> bool:
    """Sends `text` to the owner's own WhatsApp (self-chat).

    agent_id/tier: passed straight to permissions.mint_token() - your own
    agent's id and its own dedicated tier (e.g. 'my_agent_service'), not a
    shared one. That tier's `allow` list needs both
    'mcp.whatsapp.get_own_phone' and 'mcp.whatsapp.send_message' or this
    always returns False - see permissions_config.json's existing
    *_service tiers for the exact shape to copy.

    Returns True on send, False otherwise - the owner's phone couldn't be
    resolved, the token's tier isn't allowed one of the two calls, or
    whatsapp_mcp/OpenWA is unreachable. Never raises: per this mesh's
    whatsapp-silent-on-failure rule, a notification that can't be sent
    should disappear quietly, not surface as an unhandled exception
    somewhere the caller wasn't expecting one."""
    token = permissions.mint_token(agent_id, tier)
    try:
        phone_result = await call_tool(WHATSAPP_MCP_URL, 'get_own_phone', {}, token=token)
        own_phone = phone_result.get('phone')
        if not own_phone:
            logger.warning(f"[{agent_id}] Could not resolve the owner's own phone number - nothing sent")
            return False

        chat_id = f'{_digits_only(own_phone)}@c.us'
        await call_tool(WHATSAPP_MCP_URL, 'send_message', {'chat_id': chat_id, 'text': text}, token=token)
        return True
    except Exception as e:
        logger.warning(f"[{agent_id}] Could not notify the owner: {e}")
        return False
