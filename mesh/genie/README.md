# Genie

Gives a child short Socratic hints for Daily Practice app questions, never the answer. Called only by the Daily Practice app.

Generated from `mesh/example_agent` by `python -m mesh.tools.new_agent`.

## Run it

```bash
python -m mesh.genie.server
```

The first run self-registers this agent into the `run_the_agent`
collection; every `mesh/start_all.sh` after that launches it
automatically. Port 8442.

## What to fill in

- `skills/give_hint.py` - the actual logic (has a TODO).
- `agent_executor.py` - the `GiveHintParams` field descriptions (TODOs).
- `skills_catalog.py` - tighten the skill description if routing is fuzzy.
