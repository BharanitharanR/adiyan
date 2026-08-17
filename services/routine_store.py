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

Matching isn't exact-name-only: a routine's (name + description) is also
embedded with the same local model already used for coaching memory
(nomic-embed-text via Ollama - see core/memory_index.py) and compared by
cosine similarity, so "office_attendance_check" still finds an existing
"Daily Office Check" routine even though the strings don't match at all.
Embedding is best-effort - if Ollama is briefly unreachable, the routine is
still created/indexed, just without semantic matching until it's next saved.
"""
import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROUTINES_DIR = Path.home() / '.Adiyan' / 'routines'

# Cosine similarity floor for treating two routines as "the same thing".
# Empirically calibrated against this project's own nomic-embed-text/Ollama
# setup (short name+description strings score meaningfully lower in absolute
# terms than intuition from other embedding contexts suggests): two live
# genuine-match tests scored 0.77 and 0.85 against their real counterparts,
# while a genuinely unrelated request topped out at 0.58 against the entire
# existing library. 0.72 sits in that gap. Re-tune from real usage, not
# guesswork - if a real duplicate is being missed or an unrelated routine is
# firing, check the actual score (find_similar_routine callers can log it)
# before moving this number.
SIMILARITY_THRESHOLD = 0.72


def compute_embedding(name: str, description: str, ollama_url: str) -> Optional[List[float]]:
    try:
        from llama_index.embeddings.ollama import OllamaEmbedding
        embed_model = OllamaEmbedding(model_name='nomic-embed-text', base_url=ollama_url)
        return embed_model.get_text_embedding(f"{name}: {description}")
    except Exception:
        return None


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def find_similar_routine(name: str, description: str, ollama_url: str,
                          threshold: float = SIMILARITY_THRESHOLD) -> Optional[Dict[str, Any]]:
    """Returns the closest existing routine by (name + description) meaning, if
    it's above threshold - or None if nothing's close enough (including if
    embedding fails, which degrades to "no semantic match found" rather than an
    error, matching the rest of this codebase's graceful-degradation posture for
    optional local-model capabilities)."""
    query_embedding = compute_embedding(name, description, ollama_url)
    if not query_embedding:
        return None

    import config.database as db
    best_match, best_score = None, 0.0
    for routine in db.list_routines():
        if not routine.get('embedding'):
            continue
        score = _cosine_similarity(query_embedding, routine['embedding'])
        if score > best_score:
            best_match, best_score = routine, score

    return best_match if best_score >= threshold else None


def _normalize(s: str) -> str:
    """Lowercase with every run of whitespace/underscore/hyphen collapsed out -
    same normalization services/cron_scheduler.py's resolve_job uses, so
    "office_attendance_check" and "Office Attendance Check" compare equal
    without needing a model call."""
    return re.sub(r'[\s_-]+', '', s.strip().lower())


def resolve_routine(identifier: str, ollama_url: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Finds a routine by exact name, normalized name, or (if ollama_url is
    given) semantic similarity - same three-tier matching services/cron_scheduler.py's
    resolve_job uses for jobs, so "find the office check routine" works as well
    as the exact stored name does. Returns (routine, None) or (None, error)."""
    identifier = (identifier or '').strip()
    if not identifier:
        return None, "No routine name given"

    import config.database as db
    all_routines = db.list_routines()

    matches = [r for r in all_routines if r['name'].lower() == identifier.lower()]
    if not matches:
        normalized = _normalize(identifier)
        matches = [r for r in all_routines if _normalize(r['name']) == normalized]
    if not matches and ollama_url:
        match = find_similar_routine(identifier, '', ollama_url, threshold=0.60)
        if match:
            matches = [match]

    if not matches:
        return None, f"No routine named '{identifier}'"
    return matches[0], None


def get_full_details(routine_index_row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Merges a routines-table index row (name/description/timestamps) with its
    full file content (schedule/target/instructions/etc) - the index alone
    never has enough to actually explain what a routine does."""
    file_details = read_routine_file(routine_file_path(routine_index_row['name']))
    if not file_details:
        return None
    return {
        **file_details,
        'created_at': routine_index_row.get('created_at'),
        'updated_at': routine_index_row.get('updated_at'),
    }


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
