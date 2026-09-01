"""
offload's real body - "my machine is full, find someone and route to
them." Picks the freshest-fitting known peer (db.pick_peer() skips
anyone not heard from recently - see its own docstring), calls their
run_inference skill over real A2A, and returns the completion plus
which peer actually served it.

No token minted for that call, on purpose - a genuinely different
person's Adiyan install signs its own tokens with its own
PERMISSIONS_JWT_SECRET, which this instance has no way to verify at
all, so a token here would be theater, not a real credential. The trust
boundary is what run_inference exposes (see its own docstring and
mesh/compute_share/agent_executor.py's PUBLIC_SKILLS), not who's
calling it - matching the deliberate, documented design choice in
README.md (no peer authentication, the same stance BitTorrent takes:
verify content, not identity).
"""
from typing import Any, Dict, Optional

from mesh.compute_share import db
from mesh.compute_share.constants import STORAGE_ID
from mesh.lib.a2a_client import call_agent
from mesh.lib.paths import state_db_path


class NoPeerAvailableError(Exception):
    pass


async def run(prompt: str, model: Optional[str] = None) -> Dict[str, Any]:
    conn = db.connect(state_db_path(STORAGE_ID))
    peer = db.pick_peer(conn)
    if peer is None:
        raise NoPeerAvailableError('No peer has announced availability - nothing to offload to.')

    params: Dict[str, Any] = {'prompt': prompt}
    if model or peer.get('model'):
        params['model'] = model or peer['model']

    result = await call_agent(peer['peer_url'], 'run_inference', params)
    return {
        'completion': result.get('completion'),
        'served_by': peer['peer_url'],
        'model': result.get('model'),
    }
