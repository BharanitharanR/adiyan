"""
check_availability's real body - "can you take a request right now,"
answered honestly and cheaply, with no side effect. Kept as a real A2A
skill for any caller that goes through the normal agent-to-agent path,
but mesh/compute_share/skills/offload.py's own availability race hits
mesh/compute_share/availability.py's raw HTTP listener directly instead
- see that module's own docstring for why a genuinely separate thread
matters for this specific check.

Public, same as run_inference (see agent_executor.py's PUBLIC_SKILLS) -
a peer asking "are you free" before deciding whether to send real work
needs an answer before it can hold any credential this instance could
verify anyway, and there's nothing here to leak: a boolean, nothing else.
"""
from typing import Any, Dict

from mesh.compute_share import availability


async def run() -> Dict[str, Any]:
    return {'available': availability.is_available()}
