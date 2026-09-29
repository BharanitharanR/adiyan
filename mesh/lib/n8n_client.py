"""
Minimal n8n REST API client - create a workflow and activate it, nothing
else. Used by apply_vertical_spec.py to turn a spec's plain-English
`workflows:` entries into real, running n8n workflows with no human ever
opening n8n's own editor.

Authenticates as the auto-provisioned owner account (see
mesh/tools/provision_n8n_owner.js's own docstring for why that account
exists and how its credentials land on disk) - the same login flow used by
hand once already this session to build the first real workflow
(mesh/tools/new_order_workflow_backup.json), now made reusable instead of
being a one-off curl sequence.

n8n's own "publish"/version model (confirmed live: a plain
PATCH {"active": true} returns 200 but does NOT actually activate a
workflow in this n8n version, 2.35.7) means activation is a genuinely
separate step from creation - POST /rest/workflows/{id}/activate with the
just-created workflow's own versionId, not a boolean flag on an update.
"""
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger('N8NClient')

N8N_EDITOR_URL = 'http://localhost:5678'
_CREDENTIALS_PATH = Path.home() / '.Adiyan' / 'n8n_owner_credentials.json'


class N8NError(Exception):
    """Raised for any n8n API failure - login, create, or activate. Caught
    once by apply_vertical_spec.py's own best-effort loop, same reasoning
    as ingest_book's own best-effort failure handling elsewhere in this
    mesh: one workflow failing to generate shouldn't block the rest of the
    spec, or the others, from applying."""


def _load_credentials() -> Dict[str, str]:
    if not _CREDENTIALS_PATH.exists():
        raise N8NError(f'No n8n owner credentials at {_CREDENTIALS_PATH} - has n8n ever been started?')
    return json.loads(_CREDENTIALS_PATH.read_text())


async def _login(client: httpx.AsyncClient) -> str:
    """Returns the raw n8n-auth cookie value, for the caller to attach as
    an explicit `Cookie` header on every subsequent request - NOT relying
    on httpx's own cookie jar to resend it automatically. n8n sets this
    cookie with the `Secure` attribute even on a plain http:// connection
    (confirmed live: `Set-Cookie: n8n-auth=...; Secure; ...`), and httpx's
    jar correctly refuses to re-attach a Secure cookie to a non-https
    request - every follow-up call came back 401 Unauthorized despite a
    successful login, with the cookie sitting right there in
    client.cookies. n8n itself is only ever reached over plain HTTP on
    localhost in this deployment (see N8N_EDITOR_URL) - deliberately
    bypassing that scheme check here, not a real security gap, since
    nothing about this connection is actually crossing an insecure network."""
    creds = _load_credentials()
    response = await client.post('/rest/login', json={
        'emailOrLdapLoginId': creds['email'], 'password': creds['password'],
    })
    if response.status_code != 200:
        raise N8NError(f'n8n login failed ({response.status_code}): {response.text}')
    token = response.cookies.get('n8n-auth')
    if not token:
        raise N8NError('n8n login succeeded but returned no n8n-auth cookie.')
    return token


async def create_and_activate_workflow(
    name: str, nodes: List[Dict[str, Any]], connections: Dict[str, Any], settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Creates a workflow, then activates it - returns
    {'workflow_id': ..., 'active': True}. Raises N8NError on any failure
    (login, create, or activate) - the caller decides what "one workflow
    couldn't be generated" means for the rest of its own operation, this
    function itself never partially succeeds silently."""
    async with httpx.AsyncClient(base_url=N8N_EDITOR_URL, timeout=20) as client:
        token = await _login(client)
        auth_headers = {'Cookie': f'n8n-auth={token}'}

        payload = {
            'name': name, 'nodes': nodes, 'connections': connections,
            'settings': settings or {'executionOrder': 'v1'},
        }
        create_response = await client.post('/rest/workflows', json=payload, headers=auth_headers)
        if create_response.status_code not in (200, 201):
            raise N8NError(f'n8n workflow creation failed ({create_response.status_code}): {create_response.text}')
        created = create_response.json()['data']
        workflow_id = created['id']
        version_id = created['versionId']

        activate_response = await client.post(
            f'/rest/workflows/{workflow_id}/activate', json={'versionId': version_id}, headers=auth_headers,
        )
        if activate_response.status_code != 200:
            raise N8NError(f'n8n workflow activation failed ({activate_response.status_code}): {activate_response.text}')

    return {'workflow_id': workflow_id, 'active': True}
