"""
update_stage_config's real body - the dashboard's structured stage-settings
editor (model/temperature/timeout), not reachable via free-text/WhatsApp -
see query_config.py's own module docstring on why a stage's structured
shape needs a real form, not fuzzy NL extraction. DataPart-only, not
advertised in SKILLS.
"""
from typing import Any, Dict

from mesh.lib import config_sdk


async def run(agent_id: str, stage_name: str, model: str, temperature: float, timeout: int) -> Dict[str, Any]:
    known_agents = await config_sdk.list_agent_ids()
    if agent_id not in known_agents:
        return {'updated': False, 'message': f'{agent_id!r} has no configuration on file.', 'known_agents': known_agents}

    value = {'model': model, 'temperature': temperature, 'timeout': timeout}
    ok = await config_sdk.set_stage_config(agent_id, stage_name, value)
    if not ok:
        return {'updated': False, 'message': 'Could not write the update - the config store may be unreachable.'}
    return {'updated': True, 'agent_id': agent_id, 'stage': stage_name, 'value': value}
