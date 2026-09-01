"""
offload's real body - "my machine is full, find someone and route to
them." db.pick_peers() only answers liveness ("heard from recently"),
never current load - so this races a handful of live candidates on
check_availability first, and sends the real prompt to whichever one
answers "free" first. First-to-confirm wins; the rest are cancelled,
never billed for real work they didn't end up doing.

No token minted for the winning run_inference call, on purpose - a
genuinely different person's Adiyan install signs its own tokens with
its own PERMISSIONS_JWT_SECRET, which this instance has no way to
verify at all, so a token here would be theater, not a real credential.
The trust boundary is what run_inference/check_availability expose (see
their own docstrings and mesh/compute_share/agent_executor.py's
PUBLIC_SKILLS), not who's calling them - matching the deliberate,
documented design choice in README.md (no peer authentication, the same
stance BitTorrent takes: verify content, not identity).
"""
import asyncio
from typing import Any, Dict, List, Optional

from mesh.compute_share import db
from mesh.compute_share.constants import STORAGE_ID
from mesh.lib.a2a_client import call_agent
from mesh.lib.paths import state_db_path

# How many live candidates get raced per offload - bounded for the same
# reason every other fanout in this agent is (db.GOSSIP_SAMPLE_SIZE,
# gossip.py's GOSSIP_FANOUT): enough to make "the first peer we tried
# happened to be busy" unlikely, not so many that one offload call means
# pinging half the known network.
RACE_CANDIDATES = 3


class NoPeerAvailableError(Exception):
    pass


async def _check_one(peer: Dict[str, Any]) -> bool:
    try:
        result = await call_agent(peer['peer_url'], 'check_availability', {})
        return bool(result.get('available'))
    except Exception:
        # Unreachable counts as "not available," not an error worth
        # surfacing - a peer that's gone offline since its last gossip
        # heartbeat should just lose the race, not blow up the whole
        # offload attempt over one stale entry.
        return False


async def _first_available(peers: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not peers:
        return None
    tasks = {asyncio.create_task(_check_one(peer)): peer for peer in peers}
    winner = None
    try:
        while tasks and winner is None:
            done, _ = await asyncio.wait(tasks.keys(), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                peer = tasks.pop(task)
                if task.result():
                    winner = peer
                    break
    finally:
        # Whoever didn't win (or never got to answer) doesn't need to
        # keep running - this is a cheap availability check, not real
        # work, so cancelling the rest costs nothing and wastes nobody's
        # cycles once an answer's already in hand.
        for task in tasks:
            task.cancel()
    return winner


async def run(prompt: str, model: Optional[str] = None) -> Dict[str, Any]:
    conn = db.connect(state_db_path(STORAGE_ID))
    candidates = db.pick_peers(conn, count=RACE_CANDIDATES)
    if not candidates:
        raise NoPeerAvailableError('No peer has announced availability - nothing to offload to.')

    # If nobody in the race actually confirms free (all busy, or all
    # went unreachable since their last heartbeat), fall back to the
    # single freshest candidate anyway rather than giving up - the same
    # principle as mesh/inference_router/skills/complete.py's own
    # fallback: everyone being busy is a reason to prefer someone free,
    # never a reason to refuse to even try.
    winner = await _first_available(candidates) or candidates[0]

    params: Dict[str, Any] = {'prompt': prompt}
    if model or winner.get('model'):
        params['model'] = model or winner['model']

    result = await call_agent(winner['peer_url'], 'run_inference', params)
    return {
        'completion': result.get('completion'),
        'served_by': winner['peer_url'],
        'model': result.get('model'),
    }
