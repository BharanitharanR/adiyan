"""
Registers this WhatsApp MCP server's webhook with OpenWA - the mesh
equivalent of the legacy setup_openwa_webhook.py, non-interactive and using
the values this codebase actually runs with (session 'adiyan', not that
script's 'adiyan-coaching' default; /webhook/whatsapp, not the legacy
orchestrator's /webhook/openwa).

Registers against an ngrok public URL, not 127.0.0.1 directly - OpenWA's
built-in SSRF guard (penwa/src/common/security/ssrf-guard.ts) rejects
loopback/private destinations by default ("Destination address is not
allowed"), by design. Reads the current tunnel from ngrok's local API
(127.0.0.1:4040) rather than hardcoding the hostname, so this stays correct
whenever the tunnel restarts. `ngrok http 8425` must already be running.

Run once, after the 'adiyan' session shows status 'ready' in OpenWA (check
via GET /api/sessions) - registering against a session that isn't connected
yet will succeed but nothing will ever arrive.

Run from the repo root as `python -m mesh.mcp.whatsapp.register_webhook`.
"""
import httpx

from mesh.lib.secrets_vault import get_secret
from mesh.mcp.whatsapp.server import OPENWA_SESSION_NAME, OPENWA_URL, WEBHOOK_PATH

NGROK_API = 'http://127.0.0.1:4040/api/tunnels'


def _current_public_url() -> str:
    tunnels = httpx.get(NGROK_API, timeout=5).json()['tunnels']
    https_tunnel = next((t for t in tunnels if t['proto'] == 'https'), None)
    if https_tunnel is None:
        raise RuntimeError(f'No https ngrok tunnel found at {NGROK_API} - is `ngrok http 8425` running?')
    return https_tunnel['public_url']


def main() -> None:
    webhook_url = f'{_current_public_url()}{WEBHOOK_PATH}'

    api_key = get_secret('OPENWA_API_KEY')
    if not api_key:
        print('No OPENWA_API_KEY in the vault - nothing to authenticate with. Aborting.')
        return

    sessions = httpx.get(f'{OPENWA_URL}/api/sessions', headers={'X-API-Key': api_key}, timeout=10).json()
    session = next((s for s in sessions if s['name'] == OPENWA_SESSION_NAME), None)
    if session is None:
        print(f"No session named '{OPENWA_SESSION_NAME}' exists on OpenWA yet. Create/link it first.")
        return
    if session['status'] != 'ready':
        print(f"Session '{OPENWA_SESSION_NAME}' exists but status is '{session['status']}', not 'ready'. "
              f"Registering anyway is safe, but nothing will arrive until it's connected.")

    endpoint = f"{OPENWA_URL}/api/sessions/{session['id']}/webhooks"
    payload = {
        'url': webhook_url,
        # message.sent alongside message.received - fromMe sends (including
        # ones the owner types by hand into their own linked phone) never
        # fire message.received, only message.sent (see openwa_receiver.py's
        # webhook_to_message for how the two are told apart - watermark
        # presence separates Adiyan's own echoed replies from genuine
        # owner-composed self-chat/self-phone messages).
        'events': ['message.received'],
        'retryCount': 0,
    }

    # Idempotent: PUT-update a webhook already pointed at this same URL
    # instead of POSTing a duplicate - re-running this script (e.g. after
    # changing `events`) must not leave two webhooks both firing.
    existing = httpx.get(endpoint, headers={'X-API-Key': api_key}, timeout=10).json()
    match = next((w for w in existing if w['url'] == webhook_url), None)

    if match:
        response = httpx.put(f"{endpoint}/{match['id']}", json=payload, headers={'X-API-Key': api_key}, timeout=10)
        verb = 'Updated'
    else:
        response = httpx.post(endpoint, json=payload, headers={'X-API-Key': api_key}, timeout=10)
        verb = 'Registered'

    if response.status_code in (200, 201):
        print(f'{verb}: {OPENWA_SESSION_NAME!r} -> {webhook_url} (events: {payload["events"]})')
    else:
        print(f'{verb} failed ({response.status_code}): {response.text}')


if __name__ == '__main__':
    main()
