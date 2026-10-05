"""
A family mesh of genies: the same idea as compute_share's peer sharing
(mesh/compute_share/README.md) - no central server, peers found by two-way
gossip, a caller races peers and uses whichever answers first - but limited
to Adiyan installs the family owns.

What makes a peer "family":
- it knows the family device key (GENIE_DEVICE_KEY), the same key the
  tablet uses; every genie-to-genie call carries it, and
- its address is private: on the tailnet (100.64.0.0/10 or *.ts.net) or the
  home network (10/8, 172.16/12, 192.168/16). A key-holder can never make a
  genie call out to an arbitrary internet address.

Reachability across home and away networks is Tailscale's job underneath
(as compute_share's README also concludes); nothing here opens a port.

The child's data never leaves the family: unlike communitySearch, a peer is
never a stranger, so questions, answer keys and recordings stay on machines
the family owns.
"""
import asyncio
import ipaddress
import json
import shutil
import socket
import sqlite3
import subprocess
import time
import uuid
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

from mesh.genie.constants import AGENT_ID
from mesh.lib import config_sdk, permissions
from mesh.lib.mcp_client import call_tool
from mesh.lib.paths import state_db_path
from mesh.lib.secrets_vault import get_secret

WHATSAPP_MCP_URL = 'https://127.0.0.1:8425/mcp'
GATEWAY_PORT = 8081
MAX_PEERS = 16
GOSSIP_EVERY_SECONDS = 600
FORGET_AFTER_SECONDS = 14 * 24 * 3600  # a peer not heard from in two weeks is dropped
BUSY_AT = 3  # in-flight hint/listen calls at which this genie reports itself busy

_in_flight = 0
_last_gossip = 0.0
_whatsapp_cache = (0.0, False)


# ---- in-flight counter (the signal a racing caller needs, like compute_share's) ----------

class busy:
    """`async with family.busy():` around real work, so ping reports load honestly."""

    async def __aenter__(self):
        global _in_flight
        _in_flight += 1

    async def __aexit__(self, *exc):
        global _in_flight
        _in_flight = max(0, _in_flight - 1)


# ---- addresses ---------------------------------------------------------------------------

def private_url(url: str) -> Optional[str]:
    """The genie URL normalised, or None if it isn't a tailnet or home-network address."""
    try:
        u = urlparse((url or '').strip())
    except ValueError:
        return None
    if u.scheme not in ('http', 'https') or not u.hostname:
        return None
    host = u.hostname.lower()
    ok = host.endswith('.ts.net')
    if not ok:
        try:
            ip = ipaddress.ip_address(host)
            ok = ip in ipaddress.ip_network('100.64.0.0/10') or ip.is_private
        except ValueError:
            ok = False
    ip = _maybe_ip(host)
    if not ok or (ip is not None and (ip.is_loopback or ip.is_link_local or ip.is_unspecified)):
        return None
    path = u.path if u.path.endswith('/') else u.path + '/'
    return f'{u.scheme}://{u.netloc}{path or "/agents/genie/"}'


def _maybe_ip(host: str):
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _tailscale_name() -> Optional[str]:
    exe = shutil.which('tailscale') or '/opt/homebrew/opt/tailscale/bin/tailscale'
    try:
        out = subprocess.run([exe, 'status', '--json'], capture_output=True, text=True, timeout=5)
        name = json.loads(out.stdout).get('Self', {}).get('DNSName', '').rstrip('.')
        return name or None
    except Exception:
        return None


async def self_url() -> str:
    """How other family members and the tablet reach this genie: the Tailscale name if
    there is one (works at home and away), otherwise a dashboard-set address."""
    configured = await config_sdk.get_constant(
        AGENT_ID, 'family_self_url', '',
        description="This genie's address for the family mesh, e.g. http://my-mac.tailXXXX.ts.net:8081/agents/genie/. Blank: worked out from Tailscale.",
    )
    if configured:
        return configured
    name = await asyncio.to_thread(_tailscale_name)
    host = name or socket.gethostname()
    return f'http://{host}:{GATEWAY_PORT}/agents/genie/'


# ---- peer table --------------------------------------------------------------------------

def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(state_db_path(AGENT_ID))
    conn.execute('CREATE TABLE IF NOT EXISTS family_peers (url TEXT PRIMARY KEY, name TEXT, whatsapp INTEGER, last_seen REAL)')
    return conn


def _remember(url: str, name: str = '', whatsapp: Optional[bool] = None) -> None:
    conn = _db()
    row = conn.execute('SELECT whatsapp FROM family_peers WHERE url = ?', (url,)).fetchone()
    wa = int(whatsapp) if whatsapp is not None else (row[0] if row else 0)
    conn.execute('INSERT OR REPLACE INTO family_peers (url, name, whatsapp, last_seen) VALUES (?, ?, ?, ?)',
                 (url, name, wa, time.time()))
    conn.execute('DELETE FROM family_peers WHERE url NOT IN (SELECT url FROM family_peers ORDER BY last_seen DESC LIMIT ?)',
                 (MAX_PEERS,))
    conn.commit()


def known_peers() -> List[Dict[str, Any]]:
    rows = _db().execute('SELECT url, name, whatsapp, last_seen FROM family_peers ORDER BY last_seen DESC').fetchall()
    return [{'url': u, 'name': n, 'whatsapp': bool(w), 'lastSeen': s} for u, n, w, s in rows]


# ---- calling another family genie --------------------------------------------------------

async def call_peer(url: str, skill: str, params: Dict[str, Any], timeout: float = 15.0) -> Dict[str, Any]:
    """The same JSON-RPC call the tablet makes, carrying the family key."""
    body = {'jsonrpc': '2.0', 'id': str(uuid.uuid4()), 'method': 'SendMessage',
            'params': {'message': {'messageId': str(uuid.uuid4()), 'role': 'ROLE_USER',
                                   'parts': [{'data': {'skill_id': skill, **params}}]},
                       'configuration': {'historyLength': 0},
                       'metadata': {'device_key': get_secret('GENIE_DEVICE_KEY') or ''}}}
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(url, json=body, headers={'A2A-Version': '1.0'})
    r.raise_for_status()
    res = r.json()
    if res.get('error'):
        raise RuntimeError(res['error'].get('message', 'error'))
    task = res['result'].get('task', res['result'])
    if task.get('status', {}).get('state') != 'TASK_STATE_COMPLETED' or not task.get('artifacts'):
        raise RuntimeError(f"peer {url} couldn't do {skill}")
    return task['artifacts'][0]['parts'][0]['data']


# ---- WhatsApp capability -----------------------------------------------------------------

async def whatsapp_ready() -> bool:
    """True if this Adiyan's own WhatsApp link is up (cached for a minute)."""
    global _whatsapp_cache
    at, ok = _whatsapp_cache
    if time.time() - at < 60:
        return ok
    try:
        token = permissions.mint_token(AGENT_ID, 'genie_service')
        result = await call_tool(WHATSAPP_MCP_URL, 'get_own_phone', {}, token=token)
        ok = bool(result and (result.get('phone') if isinstance(result, dict) else result))
    except Exception:
        ok = False
    _whatsapp_cache = (time.time(), ok)
    return ok


async def forward_to_whatsapp_peer(skill: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Runs a parent skill on the first family peer that has WhatsApp; None if none could."""
    candidates = [p for p in known_peers() if p['whatsapp']] + [p for p in known_peers() if not p['whatsapp']]
    me = await self_url()
    for p in candidates:
        if p['url'] == me:
            continue
        try:
            return await call_peer(p['url'], skill, {**params, 'forwarded': True}, timeout=30)
        except Exception:
            continue
    return None


# ---- skills ------------------------------------------------------------------------------

async def ping() -> Dict[str, Any]:
    return {'available': _in_flight < BUSY_AT, 'inFlight': _in_flight, 'name': socket.gethostname(),
            'url': await self_url(), 'whatsapp': await whatsapp_ready()}


async def announce(url: str = '', name: str = '', whatsapp: bool = False,
                   peers: Optional[List[str]] = None) -> Dict[str, Any]:
    """Two-way gossip, the same shape as compute_share's announce_peer: remember the caller
    and any family peers it knows, and hand back a sample of what this genie knows."""
    me = await self_url()
    caller = private_url(url)
    if caller and caller != me:
        _remember(caller, name, whatsapp)
    for p in peers or []:
        u = private_url(p)
        if u and u != me and u not in {k['url'] for k in known_peers()}:
            _remember(u)
    return {'url': me, 'name': socket.gethostname(), 'whatsapp': await whatsapp_ready(),
            'peers': [p['url'] for p in known_peers()][:MAX_PEERS]}


async def gossip(force: bool = False) -> int:
    """This genie's own outbound half: re-announce to known peers, at most every 10 minutes."""
    global _last_gossip
    if not force and time.time() - _last_gossip < GOSSIP_EVERY_SECONDS:
        return 0
    _last_gossip = time.time()
    mine = await announce()
    reached = 0
    for p in known_peers():
        try:
            reply = await call_peer(p['url'], 'announce', {'url': mine['url'], 'name': mine['name'],
                                                           'whatsapp': mine['whatsapp'], 'peers': mine['peers']}, timeout=8)
            _remember(p['url'], reply.get('name', ''), reply.get('whatsapp'))
            for u in reply.get('peers') or []:
                u = private_url(u)
                if u and u != mine['url'] and u not in {k['url'] for k in known_peers()}:
                    _remember(u)
            reached += 1
        except Exception:
            if time.time() - (p.get('lastSeen') or 0) > FORGET_AFTER_SECONDS:
                conn = _db()
                conn.execute('DELETE FROM family_peers WHERE url = ?', (p['url'],))
                conn.commit()
            continue
    return reached


async def peers() -> Dict[str, Any]:
    """Every family genie this one knows, itself first - what the tablet uses to fill its list."""
    asyncio.get_running_loop().create_task(gossip())  # refresh in the background, never block the tablet
    me = await ping()
    return {'peers': [{'url': me['url'], 'name': me['name'], 'whatsapp': me['whatsapp'], 'self': True}]
            + [{'url': p['url'], 'name': p['name'], 'whatsapp': p['whatsapp']} for p in known_peers()]}
