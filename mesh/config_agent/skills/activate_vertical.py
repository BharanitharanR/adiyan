"""
activate_vertical's real body. Under phrase-based routing (see
mesh/orchestrator/rules_engine.py's _resolve_summoned_vertical()), a
vertical with any configuration on file is reachable by its own
summon_phrase automatically - there's no separate global "activation" step
a freshly-applied spec needs anymore (apply_vertical_spec.py's own write is
enough). This skill's real remaining job is re-enabling a vertical that was
previously deactivate_vertical'd (vertical_enabled set back to True),
leaving its stored summon_phrase and every other constant untouched -
recovering never needs the phrase retyped or the spec re-uploaded.
Confirmed by whoever calls this (owner via WhatsApp free text, support via
the dashboard's structured call) - the call itself IS the confirmation, no
separate yes/no round trip. Refuses a vertical_id nothing has ever
configured, so a typo doesn't silently create a phantom vertical.
"""
from typing import Any, Dict, Optional

from mesh.lib import config_sdk


async def _vertical_has_any_config(vertical_id: str) -> bool:
    """True if at least one agent has an override document for this
    vertical_id - checked against every known platform agent, since there's
    no single collection-wide 'distinct vertical_id' query this module
    exposes (config_sdk's schema stays private to it, even from its own
    sibling agent)."""
    for agent_id in await config_sdk.list_agent_ids():
        if vertical_id in await config_sdk.list_vertical_ids(agent_id):
            return True
    return False


async def run(vertical_id: str) -> Dict[str, Any]:
    if not await _vertical_has_any_config(vertical_id):
        return {
            'activated': False,
            'message': f"No agent has any configuration for vertical {vertical_id!r} - nothing to activate.",
        }
    ok = await config_sdk.set_constant('orchestrator', 'vertical_enabled', True, vertical_id=vertical_id)
    if not ok:
        return {'activated': False, 'message': 'Could not write the activation - the config store may be unreachable.'}
    return {'activated': True, 'vertical_id': vertical_id}
