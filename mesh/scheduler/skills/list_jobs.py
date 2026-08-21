"""list_jobs's real body - a plain read of this agent's own state.db. No LLM
call, no MCP call, no cross-boundary dependency - the simplest of the three
skills, and the only one with nothing left undecided."""
from typing import Any, Dict, Optional

from mesh.lib.paths import state_db_path
from mesh.scheduler import db
from mesh.scheduler.constants import AGENT_ID


# Internal-only columns a caller has no use for and shouldn't have to receive -
# the embedding is a several-hundred-float dedup key, not job information.
_INTERNAL_FIELDS = {'embedding'}


def run(target: Optional[str] = None, status: Optional[str] = None) -> Dict[str, Any]:
    conn = db.connect(state_db_path(AGENT_ID))
    rows = conn.execute('SELECT * FROM jobs').fetchall()
    jobs = [{k: v for k, v in dict(row).items() if k not in _INTERNAL_FIELDS} for row in rows]
    if target:
        jobs = [j for j in jobs if j['target'] == target]
        # No fallback to a hardcoded 'self' here - zero matches for a real,
        # correctly-specified target is an accurate answer (count: 0), not
        # an error to paper over.
    # status isn't a stored column yet - state.db only tracks next_run_at,
    # not a job's last-run outcome. Filtering by status is accepted as a
    # parameter (matches the card's declared skill) but has no effect until
    # run history is tracked, which nothing here does yet.
    return {'count': len(jobs), 'jobs': jobs}
