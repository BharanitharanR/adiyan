"""
Starts a stopped mesh agent on demand, when mesh/lib/a2a_client.py's
_send_and_await() finds nothing listening at the URL it needs - the wake
half of Adiyan's scale-to-zero. mesh/tools/offload_idle_agents.py is the
offload half: it stops an agent that's had no real activity (per
mesh/lib/agent_activity.py) in a while; this module brings it back the
moment something actually needs it, so idling an agent never means a
dropped call - only a one-time cold-start delay on whichever call catches
it stopped.

Deliberately narrow: only ever starts a component, via the exact same
mesh/start_all.sh every manual restart already uses - this module never
touches a process directly (no kill/fork/signal of its own), so there
remains exactly one place that knows how to actually launch each
component's real command.
"""
import asyncio
import logging
import subprocess
import time
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger('process_control')

REPO_ROOT = Path(__file__).resolve().parents[2]
START_ALL = REPO_ROOT / 'mesh' / 'start_all.sh'

# Mirrors the subset of mesh/start_all.sh's own COMPONENTS list that
# mesh/tools/offload_idle_agents.py is allowed to idle-stop (see that
# module's own docstring for why orchestrator/whatsapp_mcp aren't in it -
# they're the mesh's entry point, nothing else would ever be alive to wake
# them on demand). Duplicated here rather than parsed out of the shell
# script at runtime - every agent's own constants.py already duplicates its
# own port the same way, so this isn't a new kind of coupling. Includes a
# couple of components offload_idle_agents.py never stops (memory,
# analysis, orchestrator) too, so this table doubles as a general "how do I
# start component X" lookup, not just the offloadable subset.
PORT_TO_COMPONENT: Dict[int, str] = {
    8420: 'scheduler',
    8422: 'journal',
    8423: 'memory',
    8426: 'orchestrator',
    8427: 'analysis',
    8428: 'config_agent',
    8429: 'adiyan_reader',
    8441: 'inference_router',
    8460: 'compute_share',
    8462: 'p2p',
}

_START_TIMEOUT_S = 45.0
_POLL_INTERVAL_S = 1.0

# One lock per component, keyed in-process - two concurrent calls to the
# same stopped agent wait on the same start rather than racing two
# `start_all.sh start X` invocations against each other. Per-process only
# (not cross-process), which is fine: start_all.sh's own launch is already
# idempotent (a component already running is left alone), so a second,
# redundant `start` from a different process is a harmless no-op, not a
# double-launch.
_locks: Dict[str, asyncio.Lock] = {}


def _component_for(agent_url: str) -> Optional[str]:
    try:
        port = urlparse(agent_url).port
    except Exception:
        return None
    return PORT_TO_COMPONENT.get(port)


async def _is_up(agent_url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(f'{agent_url}/.well-known/agent-card.json')
        return response.status_code == 200
    except Exception:
        return False


async def ensure_running(agent_url: str) -> bool:
    """True if agent_url now has something listening - either it already
    did (a concurrent caller won the race and started it first), or this
    call started it and it came up within the timeout. False if agent_url
    isn't a component this module knows how to start, or it didn't come up
    in time - either way the caller should let its own original request
    fail with its own real error, not have this function invent a
    different one."""
    component = _component_for(agent_url)
    if component is None:
        return False

    lock = _locks.setdefault(component, asyncio.Lock())
    async with lock:
        if await _is_up(agent_url):
            return True
        logger.warning(f"{component!r} appears to be down - starting it on demand")
        try:
            result = subprocess.run(
                ['bash', str(START_ALL), 'start', component],
                cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
            )
        except Exception as e:
            logger.warning(f"Could not run start_all.sh start {component}: {e}")
            return False
        if result.returncode != 0:
            logger.warning(f"start_all.sh start {component} failed: {result.stderr.strip()[-500:]}")
            return False

        deadline = time.monotonic() + _START_TIMEOUT_S
        while time.monotonic() < deadline:
            if await _is_up(agent_url):
                logger.info(f"{component!r} is back up")
                return True
            await asyncio.sleep(_POLL_INTERVAL_S)
        logger.warning(f"{component!r} did not come up within {_START_TIMEOUT_S}s")
        return False
