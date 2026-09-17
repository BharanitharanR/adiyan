"""
Platform-wide business-persona hook - the ask() counterpart to
mesh/lib/memory_hook.py's CURRENT_CONTEXT, same two-sided contract:
mesh/lib/bootstrap.py's executor wrapper resolves it once per incoming A2A
request and sets this; mesh/lib/agent_sdk.py's AdiyanAgent.ask() reads it -
neither file needs to know the other exists, and no signature in between
carries it. See memory_hook.py's own docstring for why this shape (a plain
ContextVar, not a threaded parameter) is the point, not an accident.

Where memory_hook.CURRENT_CONTEXT answers "what do we know about this
person," this answers "how should THIS agent sound and behave for THIS
business" - business_persona_context, config_sdk's own vertical-override
constant (see mesh/config_agent/skills/apply_vertical_spec.py for how a
business owner's uploaded spec actually gets one written). Deliberately
per-agent, not one global string: orchestrator writes the final reply text
a customer sees, analysis does the reasoning behind it, and a future agent
gets its own persona slot automatically the moment it calls
mesh/lib/bootstrap.py's serve() like every other agent already must - no
code of its own needed, the same "any agent that implements the interface"
guarantee bootstrap.py's own docstring already makes for self-registration.
Adding a `business_persona_context` entry to a new agent's own
seed_config.json (and, if the business should be able to set it, to
apply_vertical_spec.py's _ALLOWED_CONSTANTS) is the only step that agent
ever needs - reading it back happens here, for free, forever.

Always '' for the owner's own messages, regardless of what vertical is
active - a business owner talking to their own Adiyan number should never
hear their own customer-facing voice. Resolved ONCE here, in the wrapper,
not left to each caller to remember - analyze.py and humanize.py both
carried a hand-rolled version of exactly this fetch-and-prepend before this
module existed; removed once this landed, since a third agent needing the
same persona would otherwise mean a third copy of the same few lines,
which is exactly the shape of drift memory_hook.py's own docstring already
warns about for a different platform hook.
"""
import contextvars
import logging
from typing import Any, Dict, Optional

from mesh.lib import config_sdk

logger = logging.getLogger(__name__)

# Default '', not None - every reader does `if persona_context:` rather
# than an `is not None` check, mirroring memory_hook.CURRENT_CONTEXT's own
# convention exactly, for the same reason: "nothing active" and "code
# running outside any request at all" (this module's own test scripts)
# must behave identically, never crash.
CURRENT_PERSONA_CONTEXT: contextvars.ContextVar[str] = contextvars.ContextVar('adiyan_persona_context', default='')


async def resolve_persona_context(agent_id: str, claims: Optional[Dict[str, Any]]) -> str:
    """'' for the owner, for a machine/service caller (no real `tier`
    claim resolves to 'owner'), or when no vertical applies - every reader
    already treats '' as "nothing to add."

    vertical_id comes from claims['vertical_id'] - permissions.mint_token()'s
    own field, set once per message by rules_engine.check()'s phrase-registry
    resolution (which of N coexisting verticals' own summon phrase this
    message actually matched), not asked ambiently from config_sdk's legacy
    "whatever's deployment-wide active" state. Omitted (None) falls through
    to that legacy resolution unchanged - a caller that never adopts
    per-message vertical propagation keeps working exactly as before.

    Orchestrator's OWN persona (read by its own ask() calls, e.g.
    humanize()) is a special case: bootstrap.py's wrapper resolves this
    once, before handle_message.py has even parsed the incoming text, so it
    has no vertical_id yet at that point - see handle_message.py's own
    re-set of persona_hook.CURRENT_PERSONA_CONTEXT once rules_engine.check()
    actually determines it."""
    is_owner = bool(claims) and claims.get('tier') == 'owner'
    if is_owner:
        return ''
    vertical_id = (claims or {}).get('vertical_id')
    try:
        return await config_sdk.get_constant(agent_id, 'business_persona_context', '', vertical_id=vertical_id)
    except Exception as e:
        logger.warning(f'Could not resolve business_persona_context for {agent_id!r}: {e}')
        return ''
