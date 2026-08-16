"""
Routines - durable, file-backed job templates (SKILL.md-inspired).

Every AI Cron Job, once created, is also persisted as a static Markdown file
under ~/.Adiyan/routines/ (frontmatter: schedule/target/etc, body: instructions),
indexed by config/database.py's routines table (name -> file_path + description,
for fast lookup without reading every file on disk). This is deliberately
file-backed, not just a DB row: a routine is meant to be a durable,
human-readable, human-editable definition that outlives any single scheduled
cron_jobs row - the same way a SKILL.md file outlives any one invocation of it.

services/cron_scheduler.py's create flow checks this index before creating a
new job: if a routine with the same name already exists, it's triggered
immediately instead of creating a duplicate - and if the routine's live
cron_jobs row was since deleted, the job is transparently recreated from the
routine file (schedule, target, and instructions all round-trip through it)
rather than lost.
"""
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

ROUTINES_DIR = Path.home() / '.Adiyan' / 'routines'


def _slugify(name: str) -> str:
    slug = re.sub(r'[^a-zA-Z0-9_-]+', '_', name.strip()).strip('_')
    return slug or 'routine'


def routine_file_path(name: str) -> Path:
    return ROUTINES_DIR / f"{_slugify(name)}.md"


def write_routine_file(*, name: str, description: str, schedule: str, cron_expression: str,
                        target: str, instructions: str, target_group: Optional[List[str]] = None,
                        expects_response: bool = False,
                        response_window_hours: Optional[int] = None) -> Path:
    """Writes (or overwrites) this routine's definition file. cron_expression is
    stored alongside the original natural_language_schedule so re-hydrating a
    deleted cron_jobs row from this file never needs a second LLM schedule-parse
    call - the already-parsed result round-trips through the file exactly."""
    ROUTINES_DIR.mkdir(parents=True, exist_ok=True)
    path = routine_file_path(name)
    frontmatter = [
        '---',
        f'name: {name}',
        f'description: {description}',
        f'schedule: {schedule}',
        f'cron_expression: {cron_expression}',
        f'target: {target}',
        f'target_group: {",".join(target_group) if target_group else ""}',
        f'expects_response: {"true" if expects_response else "false"}',
        f'response_window_hours: {response_window_hours if response_window_hours is not None else ""}',
        '---',
        '',
    ]
    path.write_text('\n'.join(frontmatter) + instructions.strip() + '\n')
    return path


def read_routine_file(path: Path) -> Optional[Dict[str, Any]]:
    """Parses a routine file back into its fields, or None if the file is missing
    or malformed (e.g. hand-edited into something no longer starting with a
    frontmatter block) - callers treat that as "routine unavailable", not a crash."""
    if not path.exists():
        return None
    text = path.read_text()
    if not text.startswith('---'):
        return None
    parts = text.split('---', 2)
    if len(parts) < 3:
        return None
    frontmatter_raw, body = parts[1], parts[2]
    meta: Dict[str, str] = {}
    for line in frontmatter_raw.strip().splitlines():
        if ':' not in line:
            continue
        key, _, value = line.partition(':')
        meta[key.strip()] = value.strip()

    target_group = [n.strip() for n in meta.get('target_group', '').split(',') if n.strip()] or None
    response_window_hours = meta.get('response_window_hours') or ''
    return {
        'name': meta.get('name', ''),
        'description': meta.get('description', ''),
        'schedule': meta.get('schedule', ''),
        'cron_expression': meta.get('cron_expression', ''),
        'target': meta.get('target', ''),
        'target_group': target_group,
        'expects_response': meta.get('expects_response', 'false').lower() == 'true',
        'response_window_hours': int(response_window_hours) if response_window_hours.isdigit() else None,
        'instructions': body.strip(),
    }


def delete_routine_file(path: Path):
    path.unlink(missing_ok=True)
