"""
get_all_configs's real body - bulk fetch for the config dashboard's list
view. DataPart-only, not advertised in SKILLS - see mesh/memory/skills/
ingest.py's own docstring for why an internal-only skill (called by a known
caller with a known shape, not resolved from free text) stays out of the
classify pool.
"""
from typing import Any, Dict

from mesh.lib import config_sdk


async def run() -> Dict[str, Any]:
    agent_ids = await config_sdk.list_agent_ids()
    configs = {}
    for agent_id in agent_ids:
        full = await config_sdk.get_full_config(agent_id)
        if full is not None:
            configs[agent_id] = full

    # list_agent_ids() deliberately excludes CONTROL_AGENT_ID from the
    # picker it serves (WhatsApp/NLP agent-name resolution) - see its own
    # docstring. The dashboard is a different caller with a different
    # need: deployment-wide settings (e.g. the outgoing-message watermark)
    # have to be editable from somewhere, and this is that somewhere -
    # included explicitly here, not by loosening list_agent_ids() itself.
    control = await config_sdk.get_full_config(config_sdk.CONTROL_AGENT_ID)
    if control is not None:
        configs[config_sdk.CONTROL_AGENT_ID] = control

    return {'agents': configs}
