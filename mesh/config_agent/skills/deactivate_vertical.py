"""deactivate_vertical's real body. Under phrase-based routing (see
mesh/orchestrator/rules_engine.py's _resolve_summoned_vertical()), N
verticals can be live on one deployment at once - "deactivate" can no
longer mean "revert the whole deployment," since there's no longer one
single active vertical to revert. It means "this ONE named vertical stops
answering to its own summon_phrase" - set via vertical_enabled=False,
leaving the phrase, business_persona_context, and every other constant on
file untouched, so activate_vertical can bring it straight back later with
no re-upload. Requires a real vertical_id, unlike the old global toggle -
if the caller doesn't have one, get_active_vertical's own listing (or the
owner just naming the business) is how they find it. Always succeeds even
if the vertical was already disabled (idempotent - "turn this off" should
never fail just because it was already off)."""
from typing import Any, Dict

from mesh.lib import config_sdk


async def run(vertical_id: str) -> Dict[str, Any]:
    if vertical_id not in await config_sdk.list_vertical_ids('orchestrator'):
        return {
            'deactivated': False,
            'message': f"No configuration on file for vertical {vertical_id!r} - nothing to deactivate.",
        }
    ok = await config_sdk.set_constant('orchestrator', 'vertical_enabled', False, vertical_id=vertical_id)
    if not ok:
        return {'deactivated': False, 'message': 'Could not write the deactivation - the config store may be unreachable.'}
    return {'deactivated': True, 'vertical_id': vertical_id}
