"""dispatch's real body - the client role in this agent's dual purpose
(see server.py's own docstring for the worker/serving role, its other
half). Thin wrapper around mesh/p2p/p2p_app.py's discover_and_dispatch():
this file exists so mesh/inference_router/skills/complete.py calls a real
A2A skill on a real agent for its offload decision, the same separation
mesh/compute_share/skills/offload.py provided before this replaced it -
not a bare Python import reaching into another module's internals.
"""
from typing import Any, Dict, Optional

from mesh.p2p.p2p_app import discover_and_dispatch


async def run(prompt: str, model: str) -> Dict[str, Any]:
    completion: Optional[str] = await discover_and_dispatch(model, prompt, timeout=30.0)
    return {'completion': completion}
