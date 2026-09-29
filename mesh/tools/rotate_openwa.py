"""
Scheduled preventive restart of OpenWA - the node-rotation-style workaround for
a chronic, unsolved degradation: OpenWA's long-running Puppeteer/Chromium
session keeps reporting WhatsApp status as 'ready' while its webhook dispatch
to whatsapp_mcp silently starts timing out (confirmed live: openwa.log shows
repeated `[WebhookService] Webhook delivery failed ... TimeoutError` roughly
every 24-48h of uptime, e.g. 9/23, 9/24, 9/26). A dropped webhook means an
incoming WhatsApp message - including a customer's book/page upload - never
reaches Adiyan at all; nothing downstream ever sees it, so nothing downstream
ever errors either.

There's no second WhatsApp session to fail over to (a single number has one
primary linked session), so this can't be true zero-downtime rotation like a
k8s node drain. What it can do is the same underlying idea: recycle before
failure, on a schedule, at an off-peak hour, rather than waiting for a real
customer message to silently vanish.

main() is a plain synchronous function, callable directly (this module is
also run standalone via `python -m mesh.tools.rotate_openwa` for a manual
test) or scheduled - see mesh/mcp/cron_trigger/server.py's own
`openwa_daily_rotation` APScheduler job, which calls it once a day via
asyncio.to_thread. Deliberately not an OS-level cron/launchd entry - see
that job's own comment for why Adiyan's scheduling never depends on a
host-specific mechanism.
"""
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
START_ALL = REPO_ROOT / 'mesh' / 'start_all.sh'
LOG_PATH = Path.home() / '.Adiyan' / 'logs' / 'openwa_rotation.log'

OPENWA_BASE_URL = 'http://localhost:2785'
OPENWA_SESSION_NAME = 'adiyan'
READY_POLL_INTERVAL_S = 5
READY_TIMEOUT_S = 120  # observed cold-start-to-ready in openwa.log is 5-70s


def _log(line: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
    with open(LOG_PATH, 'a') as f:
        f.write(f'{stamp} {line}\n')


def _openwa_api_key() -> str:
    # Same resolution order as OpenWAService itself (mesh/lib/utilities/
    # whatsapp/openwa_service.py) - Keychain vault first.
    from mesh.lib.secrets_vault import get_secret
    key = get_secret('OPENWA_API_KEY')
    if not key:
        raise RuntimeError('OPENWA_API_KEY not found in the OS keychain')
    return key


def _session_status(api_key: str) -> str | None:
    try:
        response = httpx.get(
            f'{OPENWA_BASE_URL}/api/sessions',
            headers={'X-API-Key': api_key},
            timeout=10.0,
        )
        response.raise_for_status()
        for session in response.json():
            if session.get('name') == OPENWA_SESSION_NAME:
                return session.get('status')
        return None  # session not present yet - still booting
    except Exception as e:
        _log(f'  status check failed: {e}')
        return None


def main() -> int:
    _log('Rotation started')

    result = subprocess.run(
        ['bash', str(START_ALL), 'restart', 'openwa'],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=180,
    )
    if result.returncode != 0:
        _log(f'FAILED: start_all.sh restart openwa exited {result.returncode}')
        _log(f'  stderr: {result.stderr.strip()[-2000:]}')
        return 1

    try:
        api_key = _openwa_api_key()
    except Exception as e:
        _log(f'FAILED: {e}')
        return 1

    deadline = time.monotonic() + READY_TIMEOUT_S
    while time.monotonic() < deadline:
        status = _session_status(api_key)
        if status == 'ready':
            _log('SUCCESS: session ready after restart')
            return 0
        time.sleep(READY_POLL_INTERVAL_S)

    _log(f'FAILED: session did not reach ready within {READY_TIMEOUT_S}s (last status: {status})')
    return 1


if __name__ == '__main__':
    sys.exit(main())
