"""
run_inference's real body - what a peer actually exposes to whoever's
inference this call runs. Reachable by any caller, authenticated or not
(see mesh/compute_share/agent_executor.py's PUBLIC_SKILLS for why a
genuinely different Adiyan install can't hold a token this instance
could verify anyway) - the trust boundary is what this function exposes,
not who's calling it. Receives a fully-built prompt and returns a
completion, nothing else. No document, memory, or conversation-history
access exists on this path at all: there's nothing here to leak even by
mistake, because the function's whole signature is (prompt in, text out).

This IS the dedicated peer-serving agent - an incoming offloaded request
never touches Orchestrator or any other agent's own message-handling
pipeline anywhere; it lands here directly (via compute_share's own A2A
server) and answers back to the calling peer directly, nothing else in
the mesh involved.

Calls agent_sdk.py's ask() now, not ChatOllama directly - `community` is
always None here, deliberately, never passed through from anywhere:
this is the SERVING side of an offload, so it must always run against
this machine's own local Ollama, never try to offload again itself.
Passing community here would risk two peers ping-ponging the same
request back and forth to each other forever.

Busy/free status lives in mesh/compute_share/availability.py now, not a
plain module-level counter here - see that module's own docstring for
why: a genuinely separate thread and socket, so a peer checking
availability never gets an answer that's itself delayed by this
function's own in-flight work.
"""
from typing import Any, Dict, Optional

from mesh.compute_share import availability
from mesh.compute_share.constants import AGENT_ID
from mesh.lib.agent_sdk import AdiyanAgent

_agent = AdiyanAgent(AGENT_ID)


async def run(prompt: str, model: Optional[str] = None) -> Dict[str, Any]:
    availability.mark_busy()
    try:
        resolved_model = model or 'qwen3:8b-16k'
        completion = await _agent.ask(prompt, stage='run_inference', model=resolved_model, temperature=0.7, community=None)
        return {'completion': completion, 'model': resolved_model}
    finally:
        availability.mark_free()
