"""
check_availability's real body - "can you take a request right now,"
answered honestly and cheaply, with no side effect. This is the actual
signal mesh/compute_share/skills/offload.py's peer-selection race needs
and db.pick_peers()'s freshness filter alone never provided: liveness
("heard from recently") and availability ("free right this second") are
different questions, and only this one answers the second.

Public, same as run_inference (see agent_executor.py's PUBLIC_SKILLS) -
a peer asking "are you free" before deciding whether to send real work
needs an answer before it can hold any credential this instance could
verify anyway, and there's nothing here to leak: a boolean, nothing else.
"""
from typing import Any, Dict

from mesh.compute_share.skills import run_inference


async def run() -> Dict[str, Any]:
    return {'available': run_inference.in_flight < run_inference.LOCAL_CONCURRENCY_LIMIT}
