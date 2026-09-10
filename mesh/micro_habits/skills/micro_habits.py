"""
log_micro_habit's real body - the whole thing you write for this agent.
No permission check, no A2A wiring, no config loading here: all of that
already happened in agent_executor.py before this runs. Just the work.
"""
import random
from typing import Any, Dict
from mesh.micro_habits import db


async def run(entry: str, mood: str = None) -> Dict[str, Any]:
    entry = (entry or "").strip()
    if not entry:
        raise ValueError("Nothing to write - the habit entry was empty after stripping whitespace.")
    # 2. Write it. db.add_entry lives in your mesh/micro_habit/db.py and
    #    is the sync pymongo call into your 'micro_habit_entries' collection.
    saved = db.add_entry(db.connect(), entry=entry, mood=mood)
    # 3. Return a small dict. The executor wraps this in a DataPart and
    #    Orchestrator turns it into the WhatsApp reply - so return what
    #    makes a good confirmation.
    return {"saved": True, "entry_id": saved["id"], "entry": entry, "logged_at": saved["created_at"]}
