"""
Stops mesh agents that have gone unused for a while, to keep idle memory/CPU
usage down - the offload half of Adiyan's scale-to-zero. mesh/lib/
process_control.py is the wake half: the moment any real call needs an
offloaded agent again, mesh/lib/a2a_client.py starts it back up on demand
and retries, so offloading one is never a dropped call, only a one-time
cold-start delay on whichever call catches it stopped.

Activity comes from mesh/lib/agent_activity.py's `agent_activity` Mongo
collection, updated on every single request every agent receives (see that
module's own docstring) - never a fixed idle-vs-busy guess, always this
deployment's own real traffic.

main() is a plain synchronous function, callable directly (this module is
also run standalone via `python -m mesh.tools.offload_idle_agents` for a
manual test) or scheduled - see mesh/mcp/cron_trigger/server.py's own
`agent_offload_scan` APScheduler job, which calls it every few minutes via
asyncio.to_thread. Deliberately not an OS-level cron/launchd entry - see
that job's own comment for why Adiyan's scheduling never depends on a
host-specific mechanism.

Every action is logged to ~/.Adiyan/logs/agent_offload.log - only real
transitions (an agent actually stopped), never a no-op scan, per this
codebase's own "logging must be earned" convention.

CANDIDATES is deliberately a strict subset of mesh/lib/process_control.py's
PORT_TO_COMPONENT table, not "every agent minus orchestrator/whatsapp_mcp":
memory and analysis are excluded even though the wake mechanism would
technically recover them fine, because they're on literally every incoming
WhatsApp message's hot path (recall/remember, and reply generation) -
offloading either would just mean it gets woken back up on the very next
real conversation turn, trading no real savings for a cold-start delay on
every single message once traffic resumes. orchestrator and whatsapp_mcp
are structurally excluded regardless: they're the mesh's own entry point,
so nothing would ever be alive to wake them.
"""
import logging
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx
from pymongo import MongoClient

from mesh.lib.process_control import PORT_TO_COMPONENT

REPO_ROOT = Path(__file__).resolve().parents[2]
START_ALL = REPO_ROOT / 'mesh' / 'start_all.sh'
LOG_PATH = Path.home() / '.Adiyan' / 'logs' / 'agent_offload.log'

MONGO_URL = 'mongodb://localhost:27017'
MONGO_DB_NAME = 'adiyan_config'
COLLECTION_NAME = 'agent_activity'

IDLE_THRESHOLD = timedelta(minutes=20)

CANDIDATES = ['scheduler', 'journal', 'adiyan_reader', 'config_agent', 'inference_router', 'compute_share', 'p2p']

_COMPONENT_TO_PORT = {name: port for port, name in PORT_TO_COMPONENT.items()}


def _log(line: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
    with open(LOG_PATH, 'a') as f:
        f.write(f'{stamp} {line}\n')


def _is_up(port: int) -> bool:
    try:
        response = httpx.get(f'http://127.0.0.1:{port}/.well-known/agent-card.json', timeout=3.0)
        return response.status_code == 200
    except Exception:
        return False


def _last_active(collection, agent_id: str) -> Optional[datetime]:
    doc = collection.find_one({'_id': agent_id})
    if doc is None:
        return None
    ts = doc.get('last_active_at')
    if ts is not None and ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def main() -> None:
    client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=3000)
    collection = client[MONGO_DB_NAME][COLLECTION_NAME]
    now = datetime.now(timezone.utc)

    for name in CANDIDATES:
        port = _COMPONENT_TO_PORT[name]
        if not _is_up(port):
            continue  # already stopped (by an earlier run, or never started this session)

        last_active = _last_active(collection, name)
        if last_active is None:
            # No activity record yet for a currently-running process -
            # could be freshly started and simply hasn't served its first
            # request. Leave it alone this round rather than guess; it
            # gets a real timestamp the moment anything actually calls it,
            # and evaluated normally on the next scan after that.
            continue

        idle_for = now - last_active
        if idle_for < IDLE_THRESHOLD:
            continue

        result = subprocess.run(
            ['bash', str(START_ALL), 'stop', name],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            _log(f'Offloaded {name!r} - idle for {idle_for}')
        else:
            _log(f'FAILED to offload {name!r}: {result.stderr.strip()[-500:]}')


if __name__ == '__main__':
    main()
