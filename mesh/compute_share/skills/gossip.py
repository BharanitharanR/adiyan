"""
gossip's real body - this instance's own outbound half of peer exchange.
announce_peer.py (the inbound half) only ever runs when someone else
calls in; something has to make the outbound calls too, or a peer that
never happens to be called by anyone else never learns anything past
its own bootstrap entry.

Picks a few already-known peers, re-announces to each (refreshing this
instance's own liveness in their table, and its own liveness in ours),
and merges whatever peers come back that weren't already known. Bounded
fanout (GOSSIP_FANOUT) on purpose - gossiping to every known peer every
round doesn't converge meaningfully faster than a few, and does scale
badly as the network grows.

Self-recurring via cron_trigger, the same pattern
mesh/adiyan_reader/skills/read_next_page.py already uses for its own
nightly reading: cron_trigger fires once per registration and doesn't
know about recurrence, so whichever skill wants to run again
re-registers itself at the end of its own run, every time.
"""
import logging
from typing import Any, Dict

from mesh.compute_share import db
from mesh.compute_share.constants import AGENT_ID, AGENT_URL, INSTANCE_ID, PUBLIC_URL, STORAGE_ID
from mesh.lib import permissions
from mesh.lib.a2a_client import call_agent
from mesh.lib.mcp_client import call_tool
from mesh.lib.paths import state_db_path

logger = logging.getLogger('ComputeShareGossip')

# How many already-known peers get re-announced to each round - bounded
# for the same reason db.GOSSIP_SAMPLE_SIZE bounds what's handed out per
# call: enough for the address book to keep spreading and staleness to
# get caught, not so much that traffic grows with the whole network's
# size every round.
GOSSIP_FANOUT = 3

# How often this instance re-announces itself - also what
# db.DEFAULT_FRESHNESS_SECONDS is calibrated against (roughly 2x this).
GOSSIP_INTERVAL_SECONDS = 300

# No config_sdk here on purpose, matching server.py's own documented
# choice (no Mongo dependency for this POC) - this agent has none of
# the machinery a dashboard override would plug into elsewhere.
CRON_TRIGGER_URL = 'http://127.0.0.1:8421/mcp'
DEFAULT_MODEL = 'qwen3:8b-16k'


async def _announce_to(conn, peer: Dict[str, Any], model: str) -> None:
    try:
        result = await call_agent(peer['peer_url'], 'announce_peer', {
            'peer_url': PUBLIC_URL,
            'model': model,
            'instance_id': INSTANCE_ID,
            'known_peers': db.sample_peers(conn, exclude_instance_id=INSTANCE_ID),
        })
    except Exception:
        # A peer that's gone quiet or unreachable this round isn't a
        # failure worth surfacing - db.pick_peers()'s own freshness filter
        # is what actually stops a dead peer from being routed to;
        # gossip just doesn't get to refresh that one entry this time,
        # and it ages out on its own if it stays unreachable.
        return
    known_peers = result.get('known_peers') or []
    if known_peers:
        db.merge_peers(conn, known_peers, learned_from=peer['instance_id'])


async def run(model: str = DEFAULT_MODEL) -> Dict[str, Any]:
    conn = db.connect(state_db_path(STORAGE_ID))
    targets = db.list_peers(conn)[:GOSSIP_FANOUT]

    for peer in targets:
        await _announce_to(conn, peer, model)

    # Re-register for the next round regardless of how this one went -
    # same reasoning as read_next_page.py's own recurrence: this firing
    # having nothing to gossip about (an empty peer table, every known
    # peer unreachable) doesn't mean the next one won't.
    try:
        token = permissions.mint_token(AGENT_ID, 'service')
        await call_tool(CRON_TRIGGER_URL, 'register_trigger', {
            'job_id': f'compute_share_gossip_{STORAGE_ID}',
            'invoke_at': _next_gossip_time(),
            'target_agent_url': AGENT_URL,
            'skill_id': 'gossip',
            'params': {'model': model},
        }, token=token)
    except Exception as e:
        # Not fatal - the chain just stops recurring until something
        # restarts it - but confirmed live this session that a bare
        # `except: pass` here hides a real, easy-to-hit misconfiguration
        # (a missing/wrong permission grant) as silent non-recurrence
        # with zero trace anywhere. Logged, not swallowed.
        logger.warning(f'Could not register next gossip round: {e}')

    return {'gossiped_to': len(targets), 'known_peer_count': len(db.list_peers(conn))}


def _next_gossip_time() -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(seconds=GOSSIP_INTERVAL_SECONDS)).isoformat()
