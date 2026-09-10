# Example Agent

A real, working agent that does almost nothing (rolls a die), kept
deliberately trivial so what's boilerplate and what's actually new per
agent is obvious at a glance.

**To make a new agent, don't `cp -r` this - run the generator:**

```bash
python -m mesh.tools.new_agent <agent_id>
```

It copies this directory, asks a handful of questions (port, display
name, skill id + description + examples, the fields the skill extracts,
whether you want a Mongo collection), and rewrites every field that has
to change - imports, `AGENT_ID`, `PORT`, the executor class, the skill
id in all three places it appears, the extraction schema, the catalog.
A raw copy leaves stale `mesh.example_agent` imports and a mismatched
skill id; the generator doesn't. The rest of this file explains what
each generated file is, so you know what you're editing afterward.

## Run it

```bash
python -m mesh.example_agent.server
```

Then either call it directly for testing:

```bash
curl -s http://127.0.0.1:8440/.well-known/agent-card.json
```

...or register it with `mesh.agent_registry` so the orchestrator can route
conversations to it like any other agent.

## Getting it started automatically

You do **not** edit `mesh/start_all.sh`. On startup this agent's `server.py`
calls `config_sdk.register_runnable(AGENT_ID, module=f'mesh.{AGENT_ID}.server',
port=port)`, which writes a row into the `run_the_agent` collection. From
then on, every `mesh/start_all.sh` run reads that collection (after its own
hardcoded core mesh is up) and launches this agent too.

So the flow for a new agent is: run it once by hand (the command above),
which registers it, and it's part of the mesh from the next `start_all.sh`
onward. The module path is a convention - `mesh.<AGENT_ID>.server` - so name
your agent's directory to match its `AGENT_ID`.

To stop `start_all.sh` launching it without deleting the row, set
`enabled: false` on it in the `run_the_agent` collection (re-running the
agent won't flip that back).

## The files, and what's actually agent-specific

| File | What changes per agent |
|---|---|
| `constants.py` | `AGENT_ID`, `PORT` - two lines |
| `skills/roll_dice.py` | The whole point - your actual logic, a plain async function |
| `skills_catalog.py` | The skill's name + plain-language description (this **is** the orchestrator's routing logic) |
| `agent_executor.py` | One `BaseModel` per skill (what parameters to extract) + one `if skill_id == ...` dispatch line |
| `server.py` | `AGENT_ID`/name/description strings - otherwise identical to every other agent's |
| `runtime_config.json` | Which model/temperature/timeout each LLM stage uses - copy as-is to start |
| `seed_config.json` | Any prompt templates or constants your skill needs, editable later from the dashboard |
| `db.py` | **Only if your agent stores its own data.** Copy as-is - the collection name comes from `AGENT_ID`, so there's nothing to rename. Call `add_entry` / `list_entries` from your skill. Delete the file if your agent is stateless (roll_dice is - nothing here is wired into this agent). |

Everything else in `agent_executor.py` - the A2A task lifecycle, the
DataPart fast-path for agent-to-agent calls, the plain-language routing
fallback for human chat, the permission check - is identical across every
agent in this mesh. You copy it, you don't design it.

## What you get for free, just by following this shape

- **Routing, in plain English.** The orchestrator decides "does this
  conversation belong to Example Agent?" by matching the caller's message
  against `skills_catalog.py`'s description - the same LLM classification
  every other agent uses. You never write a regex or a keyword list.
- **Parameter extraction.** "Roll a 20 sided die" becomes
  `{"sides": 20}` automatically, matched against the `RollDiceParams`
  schema in `agent_executor.py` - you declare the shape, the harness fills
  it in from natural language.
- **Permission checking.** One line
  (`permissions.is_allowed(claims, f'{AGENT_ID}.{skill_id}')`) and this
  agent's skills are gated by the same owner/service/client tiers every
  other agent uses - see `mesh/lib/permissions_config.json`. A skill this
  agent doesn't explicitly allow for a given tier is refused automatically.
- **Config you can edit without redeploying.** Every description, prompt
  template, model choice, and constant goes through `config_sdk`, which
  means it's editable from the config dashboard (`mesh/config_server`) the
  moment this agent starts - no code change, no restart, for a wording
  tweak or a model swap.
- **Agent-to-agent calls for free.** Any other agent can call this one
  directly with `call_agent(AGENT_URL, 'roll_dice', {'sides': 20},
  token=...)` - the same A2A protocol every agent in the mesh already
  speaks, so a new agent is immediately usable by every existing one, not
  just by a human on WhatsApp.
- **A live agent card.** `/.well-known/agent-card.json` is generated for
  you from `skills_catalog.py` - nothing to hand-write or keep in sync.
- **Your own storage, if you need it.** `db.py` is a copy-as-is MongoDB
  helper: one collection per agent (named from `AGENT_ID`), created on
  first write, in the same database (`adiyan`) the rest of the deployment
  uses. `add_entry` / `get_entry` / `list_entries` / `delete_entry` are
  there; call them from your skill. Stateless agents (roll_dice) just
  delete the file. See `mesh/scheduler/db.py` for the same pattern in
  production.

## What's still yours to write

Just the logic in `skills/roll_dice.py`, the plain-language description in
`skills_catalog.py` that tells the orchestrator when to reach for it, and -
if your agent keeps data - the calls into `db.py` from that skill. That's
genuinely the whole job.
