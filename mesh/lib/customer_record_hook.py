"""
Platform-wide customer-record hook - same two-sided ContextVar contract as
memory_hook.py/persona_hook.py: mesh/lib/bootstrap.py's executor wrapper
resolves it once per incoming A2A request and sets this; mesh/lib/
agent_sdk.py's AdiyanAgent.ask() reads it - no per-agent code needed.

Where persona_hook answers "how should THIS agent sound for THIS business,"
this answers "what does Adiyan already know about THIS customer, in THIS
business's own vertical" - a rendering of mesh/lib/customer_record.py's
envelope, not the storage layer itself.

Deliberately narrower than persona_hook's "every agent, always" rollout:
this is real customer data (potentially an address, a phone number, payment
history), and force-feeding it into every agent's prompt - including ones
with no business reason to see it, like adiyan_reader's book-voice
formatting - is unnecessary exposure, not a feature. Read is scoped to
orchestrator and analysis: the two agents that actually reason about or
reply to a customer. A future agent that genuinely needs this can be added
to _EXPOSED_AGENTS deliberately, the same explicit-allowlist discipline
apply_vertical_spec.py's own _ALLOWED_CONSTANTS already uses - never opened
to "every agent" by default the way persona tone was.

Always '' for the owner's own messages and for any message with no
vertical_id resolved (rules_engine.check() found no matching business
phrase) - a platform-default conversation has no vertical-scoped customer
record to speak of.
"""
import contextvars
import logging
from typing import Any, Dict, Optional

from mesh.lib import customer_record, memory_hook

logger = logging.getLogger(__name__)

CURRENT_CUSTOMER_RECORD: contextvars.ContextVar[str] = contextvars.ContextVar(
    'adiyan_customer_record_context', default='',
)

# See this module's own docstring on why this stays a short, explicit list
# rather than "every agent" - add an agent here only when it genuinely
# reasons about or replies to a customer, not by default.
_EXPOSED_AGENTS = {'orchestrator', 'analysis'}

# Hard cap on how many characters of a stringified section get injected -
# same reasoning as mesh/analysis/skills/analyze.py's own
# observation_char_cap: a long-lived customer's record must not balloon
# every single prompt it's injected into.
_MAX_SECTION_CHARS = 1500


def _render_section(label: str, section: Dict[str, Any]) -> str:
    if not section:
        return ''
    body = ', '.join(f'{k}: {v}' for k, v in section.items())
    if len(body) > _MAX_SECTION_CHARS:
        body = body[:_MAX_SECTION_CHARS] + ' …(truncated)'
    return f'{label}: {body}'


async def resolve_customer_record_context(agent_id: str, claims: Optional[Dict[str, Any]]) -> str:
    """'' for the owner, for a machine/service caller, for an agent not in
    _EXPOSED_AGENTS, or when no vertical applies to this message - every
    reader already treats '' as "nothing to add." Upserts a blank record on
    first contact (mesh/lib/customer_record.get_or_create's own job), so a
    customer's very first message already has a record to build on, not
    just their second."""
    if agent_id not in _EXPOSED_AGENTS:
        return ''
    is_owner = bool(claims) and claims.get('tier') == 'owner'
    if is_owner:
        return ''
    vertical_id = (claims or {}).get('vertical_id')
    if not vertical_id:
        return ''
    identity_key = memory_hook.identity_from_claims(claims)
    if not identity_key:
        return ''

    try:
        record = await customer_record.get_or_create(vertical_id, identity_key)
    except Exception as e:
        logger.warning(f'Could not resolve customer record for {identity_key!r} in {vertical_id!r}: {e}')
        return ''
    if record is None:
        return ''

    # owner_section deliberately never rendered here, regardless of what it
    # contains - this hook only ever backs a CUSTOMER-facing prompt (the
    # owner's own messages already returned '' above), and owner_section
    # existing at all is the one thing this whole design promises a
    # customer can never read back.
    return _render_section('What we know about this customer', record.customer_section)
