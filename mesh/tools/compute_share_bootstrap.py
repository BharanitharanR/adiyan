#!/usr/bin/env python3
"""
The one manual step no peer-to-peer network eliminates (see
mesh/compute_share/README.md and the Peer Exchange design) - every new
participant needs exactly one already-known peer's address to join at
all. This tool is both halves of that: printing this instance's own
bootstrap string to hand to someone else, and consuming a string
someone handed you.

Print this instance's own bootstrap string, to send to whoever you want
to peer with:
    python3 -m mesh.tools.compute_share_bootstrap --mine

Consume a string someone sent you - seeds it as a known peer and kicks
off the recurring gossip chain immediately (see mesh/compute_share/
skills/gossip.py's own docstring for what happens from here):
    python3 -m mesh.tools.compute_share_bootstrap "adiyan-peer://100.87.14.9:8460?model=qwen3:8b-16k&id=7c9e..."

Run from the repo root. compute_share must already be running locally
for the consuming form - it makes one A2A call into this instance's own
gossip skill to kick off the chain, not just a local database write.
"""
import argparse
import asyncio
from urllib.parse import parse_qs, urlparse

from mesh.compute_share import db
from mesh.compute_share.constants import AGENT_ID, AGENT_URL, INSTANCE_ID, PUBLIC_URL, STORAGE_ID
from mesh.lib import permissions
from mesh.lib.a2a_client import call_agent
from mesh.lib.paths import state_db_path

DEFAULT_MODEL = 'qwen3:8b-16k'


def _own_bootstrap_string(model: str) -> str:
    netloc = PUBLIC_URL.split('://', 1)[-1]
    return f'adiyan-peer://{netloc}?model={model}&id={INSTANCE_ID}'


def _parse_magnet(magnet: str) -> dict:
    parsed = urlparse(magnet)
    if parsed.scheme != 'adiyan-peer':
        raise ValueError(f"Not an adiyan-peer:// bootstrap string (expected scheme 'adiyan-peer', got {parsed.scheme!r}): {magnet!r}")
    if not parsed.netloc:
        raise ValueError(f'Missing host:port in bootstrap string: {magnet!r}')
    qs = parse_qs(parsed.query)
    return {
        'peer_url': f'http://{parsed.netloc}',
        'model': (qs.get('model') or [DEFAULT_MODEL])[0],
        # Falls back to peer_url as a stand-in id if the string somehow
        # omits one - matches announce_peer.py's own fallback for a
        # caller that doesn't send instance_id, rather than a hard
        # failure over one missing field.
        'instance_id': (qs.get('id') or [f'http://{parsed.netloc}'])[0],
    }


async def _consume(magnet: str) -> None:
    peer = _parse_magnet(magnet)
    conn = db.connect(state_db_path(STORAGE_ID))
    db.upsert_peer(conn, peer['instance_id'], peer['peer_url'], peer['model'])
    print(f"Seeded {peer['instance_id']} ({peer['peer_url']}) as a known peer.")

    token = permissions.mint_token(AGENT_ID, 'service')
    try:
        result = await call_agent(AGENT_URL, 'gossip', {}, token=token)
    except Exception as e:
        print(f'Seeded locally, but the first gossip round failed - is compute_share running? ({e})')
        print('The next scheduled round (if one was already registered from an earlier bootstrap) will still try.')
        return
    print(f'First gossip round complete: {result}')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('magnet_url', nargs='?', help="A peer's adiyan-peer:// bootstrap string")
    parser.add_argument('--mine', action='store_true', help="Print this instance's own bootstrap string instead")
    parser.add_argument('--model', default=DEFAULT_MODEL, help=f'Model to advertise in --mine output (default: {DEFAULT_MODEL})')
    args = parser.parse_args()

    if args.mine:
        print(_own_bootstrap_string(args.model))
        return
    if not args.magnet_url:
        parser.error('Provide a bootstrap string to consume, or pass --mine to print your own.')
    asyncio.run(_consume(args.magnet_url))


if __name__ == '__main__':
    main()
