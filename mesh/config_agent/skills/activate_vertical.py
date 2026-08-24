"""
activate_vertical's real body - the deployment-wide switch. Confirmed by
whoever calls this (owner via WhatsApp free text, support via the
dashboard's structured call) - the call itself IS the confirmation, no
separate yes/no round trip. Refuses to activate a vertical_id nothing has
ever configured, so a typo doesn't silently switch the whole deployment
onto empty overrides.
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
    ok = await config_sdk.set_active_vertical_id(vertical_id)
    if not ok:
        return {'activated': False, 'message': 'Could not write the activation - the config store may be unreachable.'}
    return {'activated': True, 'vertical_id': vertical_id}
