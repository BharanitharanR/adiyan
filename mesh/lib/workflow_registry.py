"""
The one place that writes to a vertical's workflow_registry constant -
shared by mesh/config_agent/skills/register_workflow.py (an owner typing a
command by hand) and apply_vertical_spec.py (auto-generated from a spec's
own `workflows:` section) so the upsert-by-name logic exists exactly once,
not duplicated across "an owner asked for this" and "a spec asked for
this."

mesh/analysis/skills/analyze.py's trigger_workflow tool is this registry's
only reader - see that module's _build_trigger_workflow_tool() for the
list-of-dicts shape this writes: {name, webhook_path, description}.
"""
from typing import Any, Dict, List

from mesh.lib import config_sdk

_TARGET_AGENT_ID = 'analysis'
_REGISTRY_KEY = 'workflow_registry'


async def register(vertical_id: str, name: str, webhook_path: str, description: str) -> bool:
    """Upserts by name - re-registering an already-known workflow (a
    description tweak, a moved webhook path, or a spec re-applied with the
    same workflow requested again) replaces the old entry rather than
    leaving a stale duplicate for trigger_workflow to also list."""
    current: List[Dict[str, Any]] = await config_sdk.get_constant(_TARGET_AGENT_ID, _REGISTRY_KEY, [], vertical_id=vertical_id)
    updated = [w for w in current if not (isinstance(w, dict) and w.get('name') == name)]
    updated.append({'name': name, 'webhook_path': webhook_path, 'description': description})
    return await config_sdk.set_constant(_TARGET_AGENT_ID, _REGISTRY_KEY, updated, vertical_id=vertical_id)
