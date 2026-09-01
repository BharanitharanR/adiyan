"""compute_share's own domain data: the local table of peers this
instance knows are willing to take work.

Keyed by instance_id, not peer_url - confirmed necessary, not just
tidier: a peer's reachable address can change (a Tailscale IP
reassigned, a machine reconnecting) while it's still the same peer.
Keying on the address the way Phase 1 did means a reconnect looks like
a brand new, unrelated peer instead of an update to a known one.

last_seen_at is what makes gossip (mesh/compute_share/skills/
announce_peer.py) mean something: a peer that announced once and went
quiet an hour ago is worse than useless to route to - pick_peers()
filters on this, not just insertion order. learned_from is bookkeeping,
not used for routing - a peer's own first announce still overwrites it
on re-announce (see upsert_peer), so it always answers "who actually
introduced me to this peer" rather than "who mentioned them most
recently."

Deliberately still not shared/global across instances - each install
keeps its own local view, populated by direct announce and gossip
peer-exchange, never a synced/consistent table across machines. See
README.md for why a real registry (or DHT) is future work, not this."""
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS peers (
    instance_id TEXT PRIMARY KEY,
    peer_url TEXT NOT NULL,
    model TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    learned_from TEXT
)
"""

# How long a peer is trusted as "probably still alive" after its last
# announce/heartbeat before pick_peers() stops offering it. Not a hard
# science for a POC - roughly 2x a several-minute heartbeat interval,
# same reasoning as any other liveness timeout: long enough that one
# missed heartbeat (a slow network blip) doesn't false-negative a real
# peer, short enough that a peer that's actually gone doesn't linger as
# a routable option for hours.
DEFAULT_FRESHNESS_SECONDS = 600

# Cap on how many peers get handed out in one gossip exchange - bounds
# message size regardless of how large the network grows, the same
# reasoning BitTorrent's own PEX messages cap their peer list at. This
# is a sample of what's known, never the whole table.
GOSSIP_SAMPLE_SIZE = 20


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    conn.commit()
    return conn


def upsert_peer(
    conn: sqlite3.Connection, instance_id: str, peer_url: str, model: str, learned_from: Optional[str] = None,
) -> None:
    """Records or refreshes one peer. learned_from is only ever written
    on first insert (see the ON CONFLICT clause deliberately omitting
    it) - a peer re-announcing itself directly, or being re-mentioned by
    a different introducer later, doesn't overwrite who actually
    introduced it to this instance first."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        'INSERT INTO peers (instance_id, peer_url, model, first_seen_at, last_seen_at, learned_from) '
        'VALUES (?, ?, ?, ?, ?, ?) '
        'ON CONFLICT(instance_id) DO UPDATE SET '
        'peer_url = excluded.peer_url, model = excluded.model, last_seen_at = excluded.last_seen_at',
        (instance_id, peer_url, model, now, now, learned_from),
    )
    conn.commit()


def merge_peers(conn: sqlite3.Connection, peers: List[Dict[str, Any]], learned_from: str) -> int:
    """Bulk-upserts a batch received via gossip (an announce_peer call's
    own known_peers, or its response) - the actual propagation
    mechanism: every peer in the batch that wasn't already known gets
    added, tagged with who this batch came from. Returns how many were
    genuinely new, for callers that want to log/observe spread.
    Silently skips any entry missing a required field rather than
    failing the whole batch over one malformed peer - a gossip payload
    crossing a real network boundary shouldn't get to crash the
    receiver."""
    new_count = 0
    for peer in peers:
        instance_id = peer.get('instance_id')
        peer_url = peer.get('peer_url')
        model = peer.get('model')
        if not instance_id or not peer_url or not model:
            continue
        existing = conn.execute('SELECT 1 FROM peers WHERE instance_id = ?', (instance_id,)).fetchone()
        if existing is None:
            new_count += 1
        upsert_peer(conn, instance_id, peer_url, model, learned_from=learned_from)
    return new_count


def list_peers(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    return [dict(row) for row in conn.execute('SELECT * FROM peers ORDER BY last_seen_at ASC')]


def sample_peers(conn: sqlite3.Connection, exclude_instance_id: Optional[str] = None, limit: int = GOSSIP_SAMPLE_SIZE) -> List[Dict[str, Any]]:
    """A bounded sample of known peers to hand out during gossip -
    excludes exclude_instance_id so an instance never gossips a peer
    back to itself (the caller passes its own instance_id here). Most-
    recently-seen first, so a sample favors peers most likely to still
    be alive over ones this instance hasn't confirmed in a while."""
    rows = conn.execute(
        'SELECT * FROM peers WHERE instance_id != ? ORDER BY last_seen_at DESC LIMIT ?',
        (exclude_instance_id or '', limit),
    ).fetchall()
    return [dict(row) for row in rows]


def pick_peers(
    conn: sqlite3.Connection, count: int = 3, fresh_within_seconds: int = DEFAULT_FRESHNESS_SECONDS,
) -> List[Dict[str, Any]]:
    """Up to `count` candidates for offload.py's availability race - not
    "the one right peer," since liveness (this function's own job) and
    availability (check_availability.py's job, answered per-candidate at
    request time) are different questions this function alone can't
    answer. Freshness first: only a peer heard from within
    fresh_within_seconds is even considered - one that's gone stale is
    skipped outright, not just deprioritized, since a guaranteed-dead
    peer isn't worth racing at all. Among fresh peers, oldest-last-seen-
    first is still the crude round-robin the original POC used, so one
    peer isn't in every race just for being top of an unordered scan."""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=fresh_within_seconds)).isoformat()
    rows = conn.execute(
        'SELECT * FROM peers WHERE last_seen_at >= ? ORDER BY last_seen_at ASC LIMIT ?', (cutoff, count),
    ).fetchall()
    return [dict(row) for row in rows]
