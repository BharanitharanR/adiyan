"""
offload's real body - "my machine is full, find someone and route to
them." Picks the least-recently-announced known peer, calls their
run_inference skill over real A2A with a token scoped to exactly that one
skill, and returns the completion plus which peer actually served it.

The scoped token is the whole trust boundary in this POC: it's minted
fresh for this one call, at the 'peer' tier, which mesh/lib/
permissions_config.json only allows onto compute_share.run_inference -
nothing else this agent (or any other) exposes. A malicious or just
buggy caller holding this exact token cannot reach any other skill with
it, on this agent or any other, confirmed by the live rejection test in
README.md rather than assumed from reading the config.
"""
from typing import Any, Dict, Optional

from mesh.compute_share import db
from mesh.compute_share.constants import STORAGE_ID
from mesh.lib import permissions
from mesh.lib.a2a_client import call_agent
from mesh.lib.paths import state_db_path


class NoPeerAvailableError(Exception):
    pass


async def run(prompt: str, model: Optional[str] = None) -> Dict[str, Any]:
    conn = db.connect(state_db_path(STORAGE_ID))
    peer = db.pick_peer(conn)
    if peer is None:
        raise NoPeerAvailableError('No peer has announced availability - nothing to offload to.')

    token = permissions.mint_token('compute_share', 'peer')
    params: Dict[str, Any] = {'prompt': prompt}
    if model or peer.get('model'):
        params['model'] = model or peer['model']

    result = await call_agent(peer['peer_url'], 'run_inference', params, token=token)
    return {
        'completion': result.get('completion'),
        'served_by': peer['peer_url'],
        'model': result.get('model'),
    }
