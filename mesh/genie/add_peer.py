"""
Bootstrap the family mesh, like mesh/tools/compute_share_bootstrap.py does for
compute_share: introduce this genie to one other family genie, and gossip
spreads the rest.

    python -m mesh.genie.add_peer http://other-mac.tailXXXX.ts.net:8081/agents/genie/
    python -m mesh.genie.add_peer --list

Both machines need the same GENIE_DEVICE_KEY in their vault.
"""
import asyncio
import sys

from mesh.genie.skills import family


async def main(argv):
    if not argv or argv[0] == '--list':
        me = await family.ping()
        print(f"this genie: {me['url']}  (WhatsApp {'yes' if me['whatsapp'] else 'no'})")
        for p in family.known_peers():
            print(f"peer:       {p['url']}  {p['name']}  (WhatsApp {'yes' if p['whatsapp'] else 'no'})")
        return 0
    url = family.private_url(argv[0])
    if not url:
        print('Only tailnet (100.x / *.ts.net) or home-network addresses can be family peers.', file=sys.stderr)
        return 1
    mine = await family.announce()
    reply = await family.call_peer(url, 'announce', {'url': mine['url'], 'name': mine['name'],
                                                     'whatsapp': mine['whatsapp'], 'peers': mine['peers']})
    family._remember(url, reply.get('name', ''), reply.get('whatsapp'))
    for u in reply.get('peers') or []:
        u = family.private_url(u)
        if u and u != mine['url']:
            family._remember(u)
    print(f"Introduced. {url} knows us now; we know {len(family.known_peers())} family peer(s).")
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main(sys.argv[1:])))
