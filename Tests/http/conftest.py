"""
Shared fixtures for the HTTP integration test suite (Tests/http/).

Requires a real, live Adiyan process already running (python3 main.py) - this
suite drives it through the real ui/control_panel_api.py test endpoints
(/api/test/owner-message, /api/test/client-message), which exercise the real
admin routing, job/routine engine, and reasoning cycle exactly as a genuine
WhatsApp message would, minus the WhatsApp transport itself. See
ui/control_panel_api.py's module comment on those two routes for exactly what
they do and don't fake.

Auth: reads the dashboard password from the OS Keychain vault the same way
the running Adiyan instance does (config/secrets_vault.py), so this suite
works unmodified whether or not a password is currently set.
"""
import sys
import uuid
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from config.secrets_vault import get_secret

BASE_URL = 'http://localhost:5001'


def _auth():
    password = get_secret('DASHBOARD_PASSWORD')
    if not password:
        return None
    username = get_secret('DASHBOARD_USERNAME') or 'owner'
    return (username, password)


@pytest.fixture(scope='session', autouse=True)
def _require_live_adiyan():
    """Skips the whole session with a clear message rather than a wall of
    connection-refused errors if Adiyan isn't running - this suite is an
    integration test against a real process, not a unit test."""
    try:
        resp = requests.get(f'{BASE_URL}/api/agents', auth=_auth(), timeout=5)
        if resp.status_code == 401:
            pytest.exit(
                'Dashboard auth rejected the vault-stored credentials - run '
                '`python3 tools/set_secret.py DASHBOARD_PASSWORD` to check/reset it.',
                returncode=1,
            )
        resp.raise_for_status()
    except requests.exceptions.ConnectionError:
        pytest.exit(
            f'Could not reach Adiyan at {BASE_URL} - start it first (python3 main.py).',
            returncode=1,
        )


@pytest.fixture
def client():
    """A requests.Session pre-configured with the dashboard's auth, scoped to
    this project's base URL via the helper methods below."""
    session = requests.Session()
    session.auth = _auth()

    class _Client:
        def get(self, path, **kw):
            return session.get(f'{BASE_URL}{path}', timeout=kw.pop('timeout', 30), **kw)

        def post(self, path, **kw):
            return session.post(f'{BASE_URL}{path}', timeout=kw.pop('timeout', 200), **kw)

        def owner_message(self, message: str) -> dict:
            """Sends message through the real owner self-chat routing (trigger
            phrase -> pending response -> admin agent) and returns
            {'sent_messages': [...]} - no real WhatsApp send happens."""
            resp = self.post('/api/test/owner-message', json={'message': message})
            resp.raise_for_status()
            return resp.json()

        def owner_reply_text(self, message: str) -> str:
            """Convenience: owner_message() then just the first captured
            reply's text, for tests that only care about the admin agent's
            answer, not the full routing envelope."""
            result = self.owner_message(message)
            sent = result.get('sent_messages') or []
            return sent[0]['message'] if sent else ''

        def client_message(self, contact_name: str, message: str) -> dict:
            """Runs a message through the real 7-agent client pipeline and
            returns the full result, including metadata.reasoning_cycle when
            the cycle engaged."""
            resp = self.post(
                '/api/test/client-message',
                json={'contact_name': contact_name, 'message': message},
            )
            resp.raise_for_status()
            return resp.json()

    return _Client()


@pytest.fixture
def unique_name():
    """A short, collision-free name for anything this test creates (a job,
    routine, or client) - suffixed with a random fragment so re-running the
    suite never collides with a previous run's leftovers or another test's."""
    def _make(prefix: str) -> str:
        return f'{prefix}_{uuid.uuid4().hex[:8]}'
    return _make


@pytest.fixture
def test_client_contact(unique_name):
    """Registers a real, whitelisted client for the duration of one test -
    the client pipeline's ValidatorAgent rejects any non-whitelisted contact
    outright (confirmed live: an unregistered test contact got silently
    ignored, "not whitelisted", producing a None response that looked like a
    pipeline bug but was actually correct opt-in enforcement working as
    designed). Deleted after the test either way."""
    import config.database as db
    name = unique_name('TestClient')
    db.add_client(name, phone=None, lid=f'{name}@test')
    yield name
    with db._connect() as conn:
        conn.execute('DELETE FROM clients WHERE contact_name = ?', (name,))
        conn.commit()


@pytest.fixture
def cleanup_routines():
    """Tests register routine names they create here; they're deleted (index
    row + file + any live job) after the test regardless of pass/fail, so
    a test run never leaves artifacts cluttering the real routines library."""
    import config.database as db
    from services.routine_store import delete_routine_file, routine_file_path
    from services.cron_scheduler import resolve_job

    created = []
    yield created
    for name in created:
        job, _ = resolve_job(name)
        if job:
            db.delete_cron_job(job['id'])
        db.delete_routine(name)
        delete_routine_file(routine_file_path(name))
