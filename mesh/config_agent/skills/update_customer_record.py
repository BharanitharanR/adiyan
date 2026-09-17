"""
update_customer_record's real body - the ONLY way a customer record's
owner_section ever gets written (see mesh/lib/customer_record.py's own
docstring on why: a customer can never write, or even see, this section,
so a payment confirmation or an active/inactive flag can only ever come
from an explicit owner command like this one, never from LLM inference on
a conversation).

Requires the customer's real phone number, not a fuzzy name - resolving a
name to an identity would need a name->identity lookup this module has no
access to (that lives in orchestrator's own clients table); a phone number
round-trips through mesh/lib/identity.py's resolve_identity_key() the same
way every other identity in this mesh does, with no cross-agent lookup
needed. Same phone-form-only limitation that function's own docstring
already states for an @lid contact - not a new gap introduced here.

vertical_id comes from the caller's own token claims (see
mesh/orchestrator/rules_engine.py's phrase-registry resolution) - the owner
issuing this command under a specific business's own wake phrase (e.g.
"@marinaspice mark Priya as paid") is what tells Adiyan which vertical's
record to update. Refuses to guess when none is present, rather than
silently writing to the wrong business's record or a bare identity_key with
no vertical scope at all.

Owner-only in practice, same as apply_vertical_spec.py - no
permissions_config.json entry needed for it specifically, since the owner
tier's own 'allow' is already ['*'] and no other tier has anything matching
'config_agent.*'.
"""
from typing import Any, Dict, Optional

from mesh.lib import customer_record
from mesh.lib.identity import resolve_identity_key


def _digits_only(phone: str) -> str:
    return ''.join(ch for ch in phone if ch.isdigit())


async def run(phone_number: str, field: str, value: str, vertical_id: Optional[str] = None) -> Dict[str, Any]:
    if not vertical_id:
        return {
            'updated': False,
            'error': (
                "No business vertical is active for this command - use that business's own wake "
                "phrase (e.g. \"@marinaspice mark Priya as paid\") so Adiyan knows which vertical's "
                "record to update."
            ),
        }
    digits = _digits_only(phone_number)
    if not digits:
        return {'updated': False, 'error': f'{phone_number!r} does not look like a real phone number.'}

    identity_key = resolve_identity_key(f'{digits}@c.us')
    ok = await customer_record.merge_owner_section(vertical_id, identity_key, {field: value})
    if not ok:
        return {'updated': False, 'error': 'Could not write the update - the config store may be unreachable.'}
    return {
        'updated': True, 'vertical_id': vertical_id, 'identity_key': identity_key, 'field': field, 'value': value,
    }
