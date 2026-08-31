"""
4-digit OTP login for the config dashboard - sent to the owner's own
WhatsApp (mesh/mcp/whatsapp/), not a stored password. One outstanding code
at a time, single-use, OTP_TTL_SECONDS (5 minutes) lifetime.

In-process state only, same reasoning mesh/lib/permissions.py's own
short-lived tokens use - nothing durable to leak, and a process restart
requiring a fresh code is the right failure mode for something this
short-lived anyway.
"""
import logging
import secrets
import time
from typing import Optional

from mesh.config_server.constants import OTP_TTL_SECONDS, WHATSAPP_MCP_URL
from mesh.lib import permissions
from mesh.lib.mcp_client import call_tool

logger = logging.getLogger('ConfigServerOTP')

_current_code: Optional[str] = None
_expires_at: float = 0.0


def _digits_only(phone: Optional[str]) -> str:
    return ''.join(ch for ch in (phone or '') if ch.isdigit())


async def send_new_code() -> bool:
    """Generates a fresh 4-digit code, invalidating any prior one, and
    sends it to the owner's own WhatsApp. True on success - False means the
    owner's phone couldn't be resolved or the message couldn't be sent
    (WhatsApp MCP down, session not ready), not that the code wasn't
    generated - either way, no valid code is left standing on failure."""
    global _current_code, _expires_at

    token = permissions.mint_token('config_server', 'config_server_otp')
    try:
        phone_result = await call_tool(WHATSAPP_MCP_URL, 'get_own_phone', {}, token=token)
        own_phone = phone_result.get('phone')
        if not own_phone:
            logger.warning('Could not resolve the owner\'s own phone number - no OTP sent')
            _current_code = None
            return False

        code = f'{secrets.randbelow(10000):04d}'
        chat_id = f'{_digits_only(own_phone)}@c.us'
        await call_tool(WHATSAPP_MCP_URL, 'send_message', {
            'chat_id': chat_id,
            'text': f'Adiyan config dashboard login code: {code} (valid for {OTP_TTL_SECONDS // 60} minutes)',
        }, token=token)

        _current_code = code
        _expires_at = time.time() + OTP_TTL_SECONDS
        return True
    except Exception as e:
        logger.warning(f'Could not send login code: {e}')
        _current_code = None
        return False


def verify(code: str) -> bool:
    """Single-use - a correct code is consumed on success, so it can't be
    replayed even within its own TTL window."""
    global _current_code
    if _current_code is None or time.time() > _expires_at:
        _current_code = None
        return False
    if code.strip() != _current_code:
        return False
    _current_code = None
    return True
