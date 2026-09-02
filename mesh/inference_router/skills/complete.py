"""
complete's real body - the one thing this whole agent exists to do:
"here's a one-off prompt, run it wherever makes sense, respond back."
This is where the actual "am I backed up" decision lives, not inside
mesh/lib/agent_sdk.py's ask() - that's the whole point of pulling it out
into its own agent instead of inline logic duplicated in every calling
process. ask() is a thin client to this skill for every plain-text call;
it never touches ChatOllama directly.

Peer offload goes through mesh/p2p/'s own A2A agent now (its `dispatch`
skill), not compute_share's own peer exchange - superseded here, see
_run_on_peer's own comment. p2p's own agent internally uses
mesh/p2p/p2p_app.py's matchmaker+UDP mechanism for the actual
cross-machine leg; this file only ever sees p2p as a normal agent-to-
agent call, same shape as talking to compute_share used to be.

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

from mesh.inference_router.constants import OLLAMA_URL, P2P_URL
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


async def _run_on_peer(prompt: str, model: str) -> Optional[str]:
    # inference_router's own identity calling p2p, not the original
    # caller's - offloading is this agent's decision, not something the
    # agent that called complete() authorized directly. Same shape this
    # call had against compute_share before p2p replaced it.
    token = permissions.mint_token('inference_router', 'inference_router_service')
    result = await call_agent(P2P_URL, 'dispatch', {'prompt': prompt, 'model': model}, token=token)
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
    except Exception as e:
        # Deliberately a blanket except, not a growing list of specific
        # exception types - real bug, confirmed live, twice: first
        # `except ConnectionError` alone never matched httpx.ConnectError
        # (langchain_ollama's async chat call goes through a STREAMING
        # code path - _achat_stream_with_aggregation ->
        # self._client.stream(...) - that the `ollama` package's own
        # ConnectError -> ConnectionError conversion only covers for its
        # separate non-streaming _request_raw() method, never for this
        # one), so the whole "try a peer" fallback silently never fired,
        # on any machine, any time Ollama was actually down. Adding
        # httpx.ConnectError explicitly was the next fix - correct, but
        # exactly the kind of whack-a-mole this dependency chain (three
        # libraries deep: langchain_ollama -> ollama -> httpx) can keep
        # producing new exception shapes for. Any failure reaching local
        # Ollama is reason enough to try a peer instead - there's no
        # real failure mode here a peer attempt shouldn't at least be
        # allowed to answer for. Real trust-boundary note (see
        # mesh/compute_share/README.md): this is the one path where a
        # prompt can leave this machine WITHOUT the caller having opted
        # in via the community sentinel - accepted as a deliberate
        # resilience tradeoff, not an oversight.
        logger.warning(f'{caller_agent_id!r}: local Ollama unreachable ({e!r}), trying a peer as a fallback')
        completion = await _run_on_peer(prompt, cfg['model'])
        if completion is None:
            raise
        return {'completion': completion, 'served_by': 'peer_after_local_offline'}
