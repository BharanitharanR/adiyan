"""
announce_peer's real body - a peer telling THIS instance "I'm willing to
take work, here's my address and model," and getting a slice of THIS
instance's own known-peer list back in the same call. That second half
is the actual discovery mechanism (see mesh/compute_share/README.md's
Phase 2 notes and the Peer Exchange design) - the same shape as
BitTorrent's PEX extension: two peers that already found each other
exchange address books, so a new peer introduced to just one other peer
ends up hearing about everyone that peer already knew, without either
side ever talking to a central directory.
"""
from typing import Any, Dict, List, Optional

from mesh.compute_share import db
from mesh.compute_share.constants import INSTANCE_ID, STORAGE_ID
from mesh.lib.paths import state_db_path


async def run(peer_url: str, model: str, instance_id: Optional[str] = None, known_peers: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    conn = db.connect(state_db_path(STORAGE_ID))

    # instance_id is optional on the wire, not optional in practice - a
    # caller that omits it (an older client, or a hand-rolled test call)
    # still gets recorded, keyed on its own URL standing in for an id,
    # rather than the whole call failing over a missing field a real
    # compute_share client always sends.
    caller_id = instance_id or peer_url
    db.upsert_peer(conn, caller_id, peer_url, model)

    if known_peers:
        db.merge_peers(conn, known_peers, learned_from=caller_id)

    return {
        'announced': True,
        'peer_url': peer_url,
        'model': model,
        # This instance's own address book, handed back in the same
        # round trip - what actually lets a network of strangers form
        # from a single manual introduction (see README's Phase 2).
        # Excludes the caller itself and whatever it just told us, so
        # this doesn't echo a peer's own info back at it as if it were
        # news.
        'known_peers': db.sample_peers(conn, exclude_instance_id=caller_id),
    }
