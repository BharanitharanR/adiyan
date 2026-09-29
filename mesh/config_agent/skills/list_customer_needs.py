"""
list_customer_needs's real body - the read half of mesh/lib/customer_needs.py,
the event log every trigger_workflow completion now writes to (see that
module's own docstring for why it exists: a booking/order/cancellation
confirmation used to only ever exist in the one chat reply that announced
it, with nowhere for an owner to ask "show me today's bookings" afterward).

Optional phone_number narrows to one customer; optional date (YYYY-MM-DD)
narrows to one day - both may be given together. Neither is required:
asking with nothing narrows to nothing, returning every entry on file for
this vertical, oldest first.

vertical_id comes from the caller's own token claims, same convention as
update_customer_record.py/register_workflow.py/message_customer.py - refuses
to guess when none is present, same reasoning as those three: a query with
no business context has no vertical-scoped log to read.

Owner-only in practice, same as every other config_agent skill here - no
permissions_config.json entry needed for it specifically, since the owner
tier's own 'allow' is already ['*'] and no other tier has anything matching
'config_agent.*'.
"""
from typing import Any, Dict, Optional

from mesh.lib import customer_needs
from mesh.lib.identity import resolve_identity_key


def _digits_only(phone: str) -> str:
    return ''.join(ch for ch in phone if ch.isdigit())


async def run(
    phone_number: Optional[str] = None, date: Optional[str] = None, vertical_id: Optional[str] = None,
) -> Dict[str, Any]:
    if not vertical_id:
        return {
            'found': False,
            'error': (
                "No business vertical is active for this command - use that business's own wake "
                'phrase (e.g. "@marinaspice what did we book today") so Adiyan knows whose log to read.'
            ),
        }

    identity_key = None
    if phone_number:
        digits = _digits_only(phone_number)
        if not digits:
            return {'found': False, 'error': f'{phone_number!r} does not look like a real phone number.'}
        identity_key = resolve_identity_key(f'{digits}@c.us')

    entries = await customer_needs.list_needs(vertical_id, identity_key=identity_key, date=date)
    return {'found': bool(entries), 'vertical_id': vertical_id, 'count': len(entries), 'entries': entries}
