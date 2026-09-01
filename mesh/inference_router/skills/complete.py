"""
complete's real body - the one thing this whole agent exists to do:
"here's a one-off prompt, run it wherever makes sense, respond back."
This is where the actual "am I backed up" decision lives, not inside
mesh/lib/agent_sdk.py's ask() - that's the whole point of pulling it out
into its own agent instead of inline logic duplicated in every calling
process. ask() is a thin client to this skill for every plain-text call;
it never touches ChatOllama or compute_share directly anymore.

Concurrency is tracked in-process (a module-level counter, not
anything shared across restarts or processes) - deliberately simple for
what's actually a simple question: is a local Ollama call already in
flight through this one gateway right now. LOCAL_CONCURRENCY_LIMIT
matches Ollama's own real single-slot behavior confirmed live on this
machine (`llama-server ... -np 1` - one request processed at a time,
everything else queues) - past that many in-flight calls, a new one
would just wait behind the others rather than run in parallel, which is
exactly what "backed up" means here.
"""
import logging
from typing import Any, Dict, Optional

from langchain_ollama import ChatOllama

from mesh.inference_router.constants import COMPUTE_SHARE_URL, OLLAMA_URL
from mesh.lib import config_sdk, permissions
from mesh.lib.a2a_client import call_agent

logger = logging.getLogger('InferenceRouter')

# See module docstring - matches Ollama's own confirmed single-slot
# concurrency, not an arbitrary guess.
LOCAL_CONCURRENCY_LIMIT = 1

_in_flight = 0


async def _run_local(prompt: str, model: str, temperature: float) -> str:
    global _in_flight
    _in_flight += 1
    try:
        llm = ChatOllama(model=model, base_url=OLLAMA_URL, temperature=temperature)
        result = await llm.ainvoke(prompt)
        return result.content
    finally:
        _in_flight -= 1


async def _run_on_peer(prompt: str, model: str) -> str:
    # inference_router's own identity calling compute_share, not the
    # original caller's - offloading is this agent's decision, not
    # something the agent that called complete() authorized directly.
    token = permissions.mint_token('inference_router', 'inference_router_service')
    result = await call_agent(COMPUTE_SHARE_URL, 'offload', {'prompt': prompt, 'model': model}, token=token)
    return result.get('completion')


async def run(
    caller_agent_id: str, stage: str, prompt: str, model: str = 'qwen3:8b-16k',
    temperature: float = 0.4, community: Optional[str] = None,
) -> Dict[str, Any]:
    # Resolved on the CALLER's behalf, by agent_id - config_sdk's stage
    # configs are keyed on agent_id in shared storage, not on which
    # process does the lookup, so this is correct even though the
    # resolution now happens here instead of inside the caller's own
    # process. This is what keeps agent.ask()'s own dashboard-editable
    # model/temperature behavior working unchanged even though the
    # actual LLM call moved out of the caller's process entirely.
    cfg = await config_sdk.get_stage_config(caller_agent_id, stage, {'model': model, 'temperature': temperature})

    backed_up = _in_flight >= LOCAL_CONCURRENCY_LIMIT
    if backed_up and community == 'communitySearch':
        try:
            completion = await _run_on_peer(prompt, cfg['model'])
            if completion is not None:
                return {'completion': completion, 'served_by': 'peer'}
            logger.warning(f'{caller_agent_id!r}: peer offload returned no completion, falling back to local')
        except Exception as e:
            logger.warning(f'{caller_agent_id!r}: backed up and peer offload failed, running locally anyway: {e}')

    # Not backed up, or backed up but not opted into community sharing,
    # or the peer attempt above failed - run it locally regardless. Being
    # busy or having no peer available is never a reason to refuse to
    # answer; it's only ever a reason to prefer someone else if allowed to.
    try:
        completion = await _run_local(prompt, cfg['model'], cfg['temperature'])
        return {'completion': completion, 'served_by': 'local'}
    except ConnectionError:
        # Ollama itself isn't reachable at all (confirmed live on an 8GB
        # Mac after a forced shutdown mid-generation) - a stronger signal
        # than "busy": there is no local answer possible right now, period.
        # Try a peer regardless of `community`, same as the already-busy
        # path above does when opted in - being offline is never a reason
        # to refuse to answer if someone else genuinely can. Real trust-
        # boundary note (see mesh/compute_share/README.md): this is the one
        # path where a prompt can leave this machine WITHOUT the caller
        # having opted in via the community sentinel - accepted here as a
        # deliberate resilience tradeoff for the POC, not an oversight.
        logger.warning(f'{caller_agent_id!r}: local Ollama unreachable, trying a peer as a fallback')
        completion = await _run_on_peer(prompt, cfg['model'])
        if completion is None:
            raise
        return {'completion': completion, 'served_by': 'peer_after_local_offline'}
