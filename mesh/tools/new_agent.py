#!/usr/bin/env python3
"""
Scaffold a new Adiyan agent from mesh/example_agent - interactively.

    python -m mesh.tools.new_agent <agent_id>

Copies mesh/example_agent -> mesh/<agent_id> and rewrites every field that
has to change per agent, so the result imports and routes correctly with
no hand-fixing. This exists because a raw `cp -r` leaves stale
`mesh.example_agent` imports, a three-way skill-id mismatch, and a dropped
`await` on register_runnable - every one of which was hit in practice.

What it asks:
  - port (defaults to the next free one)
  - display name for the agent card
  - one-line card description
  - skill id / skill name / skill description (the routing text) / examples
  - the fields the skill extracts from a message (comma-separated; the
    first is a required str, the rest Optional[str])
  - whether the agent keeps its own Mongo collection (keeps db.py)

After it runs: `python -m mesh.<agent_id>.server` once - the agent
self-registers into `run_the_agent`, and mesh/start_all.sh picks it up
from then on.
"""
import re
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MESH = REPO_ROOT / 'mesh'
SRC = MESH / 'example_agent'

# Names that already exist as core components / reserved ids - a new agent
# must not collide with one.
RESERVED = {
    'example_agent', 'orchestrator', 'scheduler', 'memory', 'journal',
    'analysis', 'adiyan_reader', 'config_agent', 'config_server',
    'inference_router', 'p2p', 'compute_share', 'agent_registry',
    'cron_trigger', 'whatsapp', 'lib', 'tools', 'mcp', 'nginx', 'qdrant',
}


def die(msg: str) -> None:
    print(f'error: {msg}', file=sys.stderr)
    sys.exit(1)


def ask(prompt: str, default: str = '') -> str:
    suffix = f' [{default}]' if default else ''
    val = input(f'{prompt}{suffix}: ').strip()
    return val or default


def camel(snake: str) -> str:
    return ''.join(p.title() for p in snake.split('_'))


def taken_ports() -> set:
    ports = set()
    for const in MESH.rglob('constants.py'):
        for m in re.finditer(r'PORT\s*=\s*(\d{2,5})', const.read_text()):
            ports.add(int(m.group(1)))
    start_all = MESH / 'start_all.sh'
    if start_all.exists():
        for m in re.finditer(r'"\w+\|(\d{2,5})\|', start_all.read_text()):
            ports.add(int(m.group(1)))
    return ports


def next_free_port(base: int = 8442) -> int:
    taken = taken_ports()
    p = base
    while p in taken:
        p += 1
    return p


def valid_id(name: str) -> bool:
    return bool(re.fullmatch(r'[a-z][a-z0-9_]*', name)) and name.isidentifier()


# --- per-file rewrites -------------------------------------------------

def rewrite_constants(text: str, c: dict) -> str:
    text = text.replace(
        '"""Example Agent\'s own identity constants.\n\n'
        'This whole agent exists as a reference implementation - see this\n'
        'directory\'s README.md for what plugging in a new agent actually involves,\n'
        'and what the harness gives you for free just by following this shape."""',
        f'"""{c["display"]}\'s own identity constants."""',
    )
    text = text.replace("AGENT_ID = 'example_agent'", f"AGENT_ID = '{c['agent_id']}'")
    text = text.replace('PORT = 8440', f'PORT = {c["port"]}')
    return text


def rewrite_server(text: str, c: dict) -> str:
    text = _rewrite_imports(text, c)
    text = text.replace('ExampleAgentExecutor', c['executor_cls'])
    text = text.replace('Example Agent - A2A server entrypoint',
                        f'{c["display"]} - A2A server entrypoint')
    text = text.replace('`python -m mesh.example_agent.server`',
                        f'`python -m mesh.{c["agent_id"]}.server`')
    text = text.replace('http://127.0.0.1:8440/.well-known/agent-card.json',
                        f'http://127.0.0.1:{c["port"]}/.well-known/agent-card.json')
    text = text.replace(
        "Nothing in this file is example-agent-specific except the two `Example`\n"
        "names and the description string - a new agent's server.py is this file\n"
        "with its own AGENT_ID/name/skills swapped in.",
        "Standard agent server.py - AGENT_ID/name/description come from\n"
        "constants.py and the config store; the rest is shared wiring.",
    )
    text = text.replace(
        '    # AGENT_ID (the README\'s "rename example_agent" step). A no-op beyond a\n',
        '    # AGENT_ID (the agent directory is named for its AGENT_ID). A no-op beyond a\n',
    )
    text = text.replace(
        "'Reference example agent - rolls a die with a given number of sides.'",
        repr(c['card_description']),
    )
    text = text.replace("name='Example Agent',", f"name={c['display']!r},")
    return text


def rewrite_skills_catalog(text: str, c: dict) -> str:
    text = _rewrite_imports(text, c)
    text = text.replace("does this\nconversation belong to Example Agent, or to someone else?",
                        f"does this\nconversation belong to {c['display']}, or to someone else?")
    text = text.replace("Example Agent's AgentSkill catalog",
                        f"{c['display']}'s AgentSkill catalog")

    desc_block = (
        "_DEFAULT_DESCRIPTIONS: Dict[str, str] = {\n"
        "    'roll_dice': \"Roll a die with a given number of sides and return the result.\",\n"
        "}"
    )
    text = text.replace(desc_block,
        "_DEFAULT_DESCRIPTIONS: Dict[str, str] = {\n"
        f"    {c['skill_id']!r}: {c['skill_description']!r},\n"
        "}")

    ex_lines = ''.join(f'        {e!r},\n' for e in c['examples'])
    ex_block = (
        "_DEFAULT_EXAMPLES: Dict[str, List[str]] = {\n"
        "    'roll_dice': [\n"
        "        'Roll a die',\n"
        "        'Roll a 20 sided die',\n"
        "        'Flip a coin',  # a coin is just a 2-sided die - the extractor maps this naturally\n"
        "    ],\n"
        "}"
    )
    text = text.replace(ex_block,
        "_DEFAULT_EXAMPLES: Dict[str, List[str]] = {\n"
        f"    {c['skill_id']!r}: [\n{ex_lines}    ],\n"
        "}")

    struct_block = (
        "_STRUCTURE: Dict[str, Dict[str, Any]] = {\n"
        "    'roll_dice': {\n"
        "        'name': 'Roll Dice', 'tags': ['example', 'random'],\n"
        "        'input_modes': ['text/plain'], 'output_modes': ['application/json'],\n"
        "    },\n"
        "}"
    )
    text = text.replace(struct_block,
        "_STRUCTURE: Dict[str, Dict[str, Any]] = {\n"
        f"    {c['skill_id']!r}: {{\n"
        f"        'name': {c['skill_name']!r}, 'tags': [{c['agent_id']!r}],\n"
        "        'input_modes': ['text/plain'], 'output_modes': ['application/json'],\n"
        "    },\n"
        "}")
    return text


def rewrite_agent_executor(text: str, c: dict) -> str:
    # Do the skill-import line BEFORE the blanket mesh.example_agent rewrite,
    # or the anchor string is already gone by the time we look for it.
    text = text.replace('from mesh.example_agent.skills import roll_dice',
                        f'from mesh.{c["agent_id"]}.skills import {c["skill_id"]}')
    text = _rewrite_imports(text, c)
    text = text.replace("Example Agent's AgentExecutor", f"{c['display']}'s AgentExecutor")
    text = text.replace('the two lines that mention roll_dice by\nname',
                        f'the lines that mention {c["skill_id"]} by\nname')
    text = text.replace('from typing import Any, Dict\n',
                        'from typing import Any, Dict, Optional\n')

    fields = c['fields']
    schema_lines = [f'    {fields[0]}: str = Field(description="TODO: what this is")']
    for f in fields[1:]:
        schema_lines.append(
            f'    {f}: Optional[str] = Field(default=None, description="TODO: what this is (optional)")'
        )
    schema_block = (
        "class RollDiceParams(BaseModel):\n"
        '    sides: int = Field(default=6, description="How many sides the die has. Default to 6 if the caller didn\'t say.")'
    )
    text = text.replace(schema_block,
        f"class {c['params_cls']}(BaseModel):\n" + "\n".join(schema_lines))

    text = text.replace("EXTRACTION_SCHEMAS = {'roll_dice': RollDiceParams}",
                        f"EXTRACTION_SCHEMAS = {{{c['skill_id']!r}: {c['params_cls']}}}")
    text = text.replace('class ExampleAgentExecutor(AgentExecutor):',
                        f'class {c["executor_cls"]}(AgentExecutor):')
    text = text.replace("if skill_id != 'roll_dice':", f"if skill_id != {c['skill_id']!r}:")
    text = text.replace('result = await roll_dice.run(**params)',
                        f'result = await {c["skill_id"]}.run(**params)')
    text = text.replace("f'Could not roll the dice: {e}'",
                        f"f'Could not run {c['skill_id']}: {{e}}'")
    return text


def rewrite_db(text: str, c: dict) -> str:
    text = text.replace('from mesh.example_agent.constants import AGENT_ID',
                        f'from mesh.{c["agent_id"]}.constants import AGENT_ID')
    text = text.replace(
        "Example Agent's own skill (roll_dice) doesn't persist anything, so nothing\n"
        "here is actually called by this agent. It's in the scaffold as the\n"
        "reference: copy this file into your agent, change AGENT_ID's value (which\n"
        "drives the collection name), and call it from your skill in skills/.",
        f"{c['display']} stores its records in the '{c['agent_id']}_entries'\n"
        "collection (the name comes from AGENT_ID). Called from skills/.",
    )
    return text


def _rewrite_imports(text: str, c: dict) -> str:
    return text.replace('mesh.example_agent', f'mesh.{c["agent_id"]}').replace(
        'mesh/example_agent', f'mesh/{c["agent_id"]}')


def build_skill_body(c: dict) -> str:
    fields = c['fields']
    sig = ', '.join([f'{fields[0]}: str'] + [f'{f}: Optional[str] = None' for f in fields[1:]])
    if c['keep_db']:
        store_args = ', '.join(f'{f}={f}' for f in fields)
        body = (
            f'    saved = db.add_entry(db.connect(), {store_args})\n'
            f'    return {{"saved": True, "id": saved["id"], "created_at": saved["created_at"]}}'
        )
        imports = f'from typing import Any, Dict, Optional\n\nfrom mesh.{c["agent_id"]} import db\n'
    else:
        ret = ', '.join(f'"{f}": {f}' for f in fields)
        body = (
            f'    # TODO: do the real work here, then return a small dict the\n'
            f'    # executor wraps in a DataPart for Orchestrator to reply with.\n'
            f'    return {{{ret}}}'
        )
        imports = 'from typing import Any, Dict, Optional\n'
    guard = (
        f'    {fields[0]} = ({fields[0]} or "").strip()\n'
        f'    if not {fields[0]}:\n'
        f'        raise ValueError("{fields[0]} was empty.")\n'
    )
    return (
        f'"""\n'
        f'{c["skill_id"]}\'s real body - the whole thing you write for this agent.\n'
        f'No permission check, no A2A wiring, no config loading here: all of that\n'
        f'already happened in agent_executor.py before this runs. Just the work.\n'
        f'"""\n'
        f'{imports}\n\n'
        f'async def run({sig}) -> Dict[str, Any]:\n'
        f'{guard}{body}\n'
    )


def build_readme(c: dict) -> str:
    return (
        f'# {c["display"]}\n\n'
        f'{c["card_description"]}\n\n'
        f'Generated from `mesh/example_agent` by `python -m mesh.tools.new_agent`.\n\n'
        f'## Run it\n\n'
        f'```bash\n'
        f'python -m mesh.{c["agent_id"]}.server\n'
        f'```\n\n'
        f'The first run self-registers this agent into the `run_the_agent`\n'
        f'collection; every `mesh/start_all.sh` after that launches it\n'
        f'automatically. Port {c["port"]}.\n\n'
        f'## What to fill in\n\n'
        f'- `skills/{c["skill_id"]}.py` - the actual logic (has a TODO).\n'
        f'- `agent_executor.py` - the `{c["params_cls"]}` field descriptions (TODOs).\n'
        f'- `skills_catalog.py` - tighten the skill description if routing is fuzzy.\n'
        + (f'- `db.py` - your `{c["agent_id"]}_entries` collection, ready to use.\n'
           if c['keep_db'] else '')
    )


def main() -> None:
    if len(sys.argv) != 2:
        die('usage: python -m mesh.tools.new_agent <agent_id>')
    agent_id = sys.argv[1].strip()
    if not valid_id(agent_id):
        die(f'{agent_id!r} is not a valid agent id (lowercase letters, digits, underscore; must start with a letter)')
    if agent_id in RESERVED:
        die(f'{agent_id!r} is a reserved / existing agent name')
    dest = MESH / agent_id
    if dest.exists():
        die(f'{dest} already exists')
    if not SRC.exists():
        die(f'scaffold {SRC} not found')

    print(f'Scaffolding mesh/{agent_id} from mesh/example_agent\n')
    c = {'agent_id': agent_id}
    c['display'] = ask('Display name (agent card)', camel(agent_id))
    c['port'] = int(ask('Port', str(next_free_port())))
    if c['port'] in taken_ports():
        die(f'port {c["port"]} is already used by another component')
    c['card_description'] = ask('One-line card description') or f'{c["display"]} agent.'
    c['skill_id'] = ask('Skill id', agent_id)
    if not valid_id(c['skill_id']):
        die(f'{c["skill_id"]!r} is not a valid skill id')
    c['skill_name'] = ask('Skill name (human-readable)', camel(c['skill_id']).replace('_', ' '))
    c['skill_description'] = ask('Skill description (this IS the routing text)')
    if not c['skill_description']:
        die('skill description is required - it is what the orchestrator routes on')
    print('Examples (how a user would phrase it) - one per line, blank line to finish:')
    examples = []
    while True:
        line = input('  > ').strip()
        if not line:
            break
        examples.append(line)
    c['examples'] = examples or [c['skill_description']]
    raw_fields = ask('Fields the skill extracts from the message (comma-separated; first is required)', 'text')
    fields = [f.strip() for f in raw_fields.split(',') if f.strip()]
    if not fields or not all(valid_id(f) for f in fields):
        die('fields must be a comma-separated list of valid identifiers')
    if len(fields) != len(set(fields)):
        die('duplicate field name')
    c['fields'] = fields
    c['keep_db'] = ask('Keep db.py (own Mongo collection)? (y/n)', 'y').lower().startswith('y')

    c['executor_cls'] = f'{camel(agent_id)}Executor'
    c['params_cls'] = f'{camel(c["skill_id"])}Params'

    # --- copy + rewrite ---
    shutil.copytree(SRC, dest, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))

    (dest / 'constants.py').write_text(rewrite_constants((SRC / 'constants.py').read_text(), c))
    (dest / 'server.py').write_text(rewrite_server((SRC / 'server.py').read_text(), c))
    (dest / 'skills_catalog.py').write_text(rewrite_skills_catalog((SRC / 'skills_catalog.py').read_text(), c))
    (dest / 'agent_executor.py').write_text(rewrite_agent_executor((SRC / 'agent_executor.py').read_text(), c))
    (dest / 'README.md').write_text(build_readme(c))

    # skill file: rename + regenerate body
    (dest / 'skills' / 'roll_dice.py').unlink()
    (dest / 'skills' / f'{c["skill_id"]}.py').write_text(build_skill_body(c))

    if c['keep_db']:
        (dest / 'db.py').write_text(rewrite_db((SRC / 'db.py').read_text(), c))
    else:
        (dest / 'db.py').unlink()

    print(f'\nCreated mesh/{agent_id}/  (port {c["port"]}, skill {c["skill_id"]!r})')
    print('\nNext:')
    print(f'  1. fill the TODOs in mesh/{agent_id}/skills/{c["skill_id"]}.py and agent_executor.py')
    print(f'  2. python -m mesh.{agent_id}.server   # runs it, and self-registers it')
    print(f'  3. from then on mesh/start_all.sh starts it automatically')


if __name__ == '__main__':
    main()
