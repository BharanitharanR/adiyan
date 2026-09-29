"""
message_customer's real body - the actual proactive-outreach capability a
business owner gets: send a message to one already-registered customer
directly, with no reply cycle behind it. Not automated, not a watchlist -
the owner composes the message and this sends exactly that, once, to
exactly the number given.

This mirrors mesh/adiyan_reader/skills/read_next_page.py's own
resolve_chat_id -> send_message_to pattern deliberately rather than
inventing a new one - that skill already proved live that reaching an
arbitrary customer's chat_id from outside a reply cycle works, scoped by
its own tier (adiyan_reader_service). This skill's tier
(config_agent_service, mesh/lib/permissions_config.json) is the identical
shape, scoped to config_agent alone.

Requires the customer's real phone number, not a fuzzy name - same
limitation update_customer_record.py already documents for itself
(resolve_identity_key's own docstring: an @lid contact's real phone number
isn't reliably resolvable, so a phone number is the only reliable input).

vertical_id comes from the caller's own token claims, same reasoning as
every other vertical-scoped owner command in this module - refuses to
guess when none is present, rather than sending under no business's
identity at all.

Owner-only in practice, same as update_customer_record.py and
register_workflow.py - no permissions_config.json entry needed for the
skill_id itself, the owner tier's own 'allow' is already ['*'].
"""
from typing import Any, Dict, Optional

from mesh.config_agent.constants import AGENT_ID
from mesh.lib.agent_sdk import AdiyanAgent

_agent = AdiyanAgent(AGENT_ID)


def _digits_only(phone: str) -> str:
    return ''.join(ch for ch in phone if ch.isdigit())


async def run(phone_number: str, message: str, vertical_id: Optional[str] = None) -> Dict[str, Any]:
    if not vertical_id:
        return {
            'sent': False,
            'error': (
                "No business vertical is active for this command - use that business's own wake "
                'phrase so Adiyan knows which business this message is being sent on behalf of.'
            ),
        }
    digits = _digits_only(phone_number)
    if not digits:
        return {'sent': False, 'error': f'{phone_number!r} does not look like a real phone number.'}

    chat_id = await _agent.resolve_chat_id(digits)
    if chat_id is None:
        return {'sent': False, 'error': f'Could not find a WhatsApp chat for {phone_number!r}.'}

    await _agent.send_message_to(chat_id, message)
    return {'sent': True, 'vertical_id': vertical_id, 'phone_number': phone_number, 'chat_id': chat_id}
