"""list_micro_habits - reads the owner's logged entries and, when there
are any, adds a short interpretation of the pattern."""
from typing import Any, Dict

from mesh.lib.agent_sdk import AdiyanAgent
from mesh.micro_habits import db
from mesh.micro_habits.constants import AGENT_ID
from mesh.lib import config_sdk

_agent = AdiyanAgent(AGENT_ID)


async def run(limit: int = 20) -> Dict[str, Any]:
    entries = db.list_entries(db.connect(), limit=limit)
    if not entries:
        return {"count": 0, "entries": [], "interpretation": None}

    lines = "\n".join(f"- {e['entry']}  ({e['created_at']})" for e in entries)
    template = await config_sdk.get_constant(
        AGENT_ID, "interpret_prompt_template",
        "Here are the owner's recent micro-habit log entries, newest first:\n"
        "{entries}\n\n"
        "In 2-3 sentences, plainly: what habits are they keeping up, what's "
        "slipping, any pattern worth noting. No pep talk, no invented detail.",
        description="How list_micro_habits summarises the entries. Needs an {entries} placeholder.",
    )
    # No memory_context handling here anymore - mesh/lib/agent_sdk.py's
    # ask() prepends the caller's known facts to this prompt automatically
    # (Phase 5's platform wiring). This skill doesn't know that happens,
    # and doesn't need to.
    prompt = template.format(entries=lines)
    interpretation = await _agent.ask(prompt, stage="interpret")
    return {"count": len(entries), "entries": entries, "interpretation": interpretation}