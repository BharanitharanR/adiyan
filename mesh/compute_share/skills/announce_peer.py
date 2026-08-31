"""
announce_peer's real body - a peer telling THIS instance "I'm willing to
take work, here's my address and model." Stands in for a tracker
announce in the POC; a real deployment would announce to a shared/public
registry instead of directly to one other instance (see README.md).
"""
from typing import Any, Dict

from mesh.compute_share import db
from mesh.compute_share.constants import STORAGE_ID
from mesh.lib.paths import state_db_path


async def run(peer_url: str, model: str) -> Dict[str, Any]:
    conn = db.connect(state_db_path(STORAGE_ID))
    db.upsert_peer(conn, peer_url, model)
    return {'announced': True, 'peer_url': peer_url, 'model': model}
