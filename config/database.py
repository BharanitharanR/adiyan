"""
Adiyan's SQLite config store: agent configs (all 13 - the 7 pipeline agents plus the 6
reasoning-cycle agents inside LLMAgent), personas, clients (replaces whitelist.txt),
ingested knowledge-base documents, and top-level settings.

Replaces pipeline.json / personas.json / whitelist.txt. A short-lived connection is
opened per call (WAL mode) rather than one held connection, matching the pattern
already used for OpenWAService's httpx client: this is accessed from several threads
(Flask, RabbitMQ consumer, OpenWA poller, KB poller, owner admin handler) and a single
shared connection across threads is the sqlite equivalent of the cross-event-loop bugs
already fixed twice this session - not worth risking a third time.
"""
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger('Database')

DATA_DIR = Path.home() / '.Adiyan'
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / 'adiyan.db'

# The canonical 13-agent registry. 'kind' distinguishes the 7 pipeline agents (LangGraph
# nodes) from the 6 reasoning-cycle agents (internal to LLMAgent) - both live in the same
# table/dashboard list, but only 'llm_stage' rows have meaningful model/temperature/
# timeout/prompt_template (the pipeline agents besides LLMAgent itself don't call a model).
PIPELINE_AGENT_DEFAULTS = {
    'parser': ('Parser Agent', 'pipeline', ['extract_message', 'get_contact_info', 'parse_lid']),
    'validator': ('Validator Agent', 'pipeline', ['check_whitelist', 'validate_format', 'check_registration']),
    'router': ('Router Agent', 'pipeline', ['load_persona', 'get_routing_rules', 'determine_flow']),
    'llm': ('LLM Agent', 'pipeline', ['call_ollama', 'get_context', 'apply_system_prompt']),
    'synthesizer': ('Synthesizer Agent', 'pipeline', ['format_response', 'split_chunks', 'apply_persona_rules']),
    'storage': ('Storage Agent', 'pipeline', ['store_in_qdrant', 'save_to_file', 'update_metadata']),
    'publisher': ('Publisher Agent', 'pipeline', ['send_whatsapp_reply', 'publish_event', 'log_completion']),
}

DEFAULT_PERSONA_ID = 'executive_coach'
DEFAULT_PERSONA_NAME = 'Executive Coach'
DEFAULT_PERSONA_PROMPT = (
    'You are an Executive Coach specializing in logical thinking and decision-making.\n\n'
    'COACHING RULES:\n'
    '1. Respond warmly and personally (not as a consultant)\n'
    '2. Provide 2-3 specific, actionable steps (not frameworks)\n'
    '3. Ask ONE probing question at the end\n'
    '4. Connect advice to their goals\n'
    '5. Ignore generic frameworks - find novel insights'
)
SYSTEM_PERSONA_PROMPT = 'You are a system assistant handling registration/unregistration.'

REASONING_CYCLE_DEFAULTS = {
    'hermes': ('Hermes (Triage)', 'Decide quick vs deep. Respond with exactly one word: "quick" or "deep".'),
    'prometheus': ('Prometheus (Planner)', 'Pick which of these steps apply, in order. '
                   'Allowed values only: ask_clarifying_question, call_tool, answer_from_knowledge_base, '
                   'verify_math, give_coaching_advice. Never invent a step outside this list.\n\n'
                   'Respond with ONLY a JSON array of the chosen step names, nothing else - no keys, '
                   'no explanation, no surrounding object. Example of the exact shape required: '
                   '["answer_from_knowledge_base"]'),
    'pythia': ('Pythia (Clarifier)', 'Judge whether a real blocking gap exists before this can be answered '
               'responsibly (e.g. missing income before financial advice). Respond with a JSON object: '
               '{"blocking": true|false, "reason": "..."}.'),
    'hephaestus': ('Hephaestus (Tools)', 'Decide whether a tool call is needed to ground the answer '
                   'before drafting. Respond with a JSON object: {"needs_tool": true|false, "reason": "..."}.'),
    'calliope': ('Calliope (Drafter)', 'Write the actual response.'),
    'momus': ('Momus (Skeptic)', 'Review the draft against the retrieved material and the plan. Respond with '
               'a JSON object: {"approved": true|false, "feedback": "..."}.'),
}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables if they don't exist, and seed default agent rows. Safe to call
    every startup - CREATE TABLE IF NOT EXISTS and INSERT OR IGNORE throughout."""
    with _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS agent_configs (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,              -- 'pipeline' or 'llm_stage'
                enabled INTEGER NOT NULL DEFAULT 1,
                model TEXT,
                temperature REAL,
                timeout INTEGER,
                prompt_template TEXT,
                tools TEXT NOT NULL DEFAULT '[]', -- json list
                retry_count INTEGER NOT NULL DEFAULT 3,
                custom_params TEXT NOT NULL DEFAULT '{}' -- json object
            );

            CREATE TABLE IF NOT EXISTS personas (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                system_prompt TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS clients (
                contact_name TEXT PRIMARY KEY,
                lid TEXT,
                phone TEXT,
                registered_at TEXT,
                last_active_at TEXT,
                is_whitelisted INTEGER NOT NULL DEFAULT 1,
                notes TEXT,
                tags TEXT NOT NULL DEFAULT '[]'  -- json list
            );

            CREATE TABLE IF NOT EXISTS kb_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                ingested_at TEXT NOT NULL,
                chunk_count INTEGER NOT NULL,
                source TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            -- Routines: durable, file-backed job templates (SKILL.md-inspired - see
            -- services/routine_store.py's module docstring). This table is only an
            -- INDEX for fast lookup (name -> file_path + description) - the actual
            -- definition (schedule/target/instructions) lives in the static file at
            -- file_path, which is the source of truth and survives even if the live
            -- cron_jobs row referencing it is later deleted. name is the shared key
            -- between a routine and the cron_jobs row(s) created from it.
            CREATE TABLE IF NOT EXISTS routines (
                name TEXT PRIMARY KEY COLLATE NOCASE,
                file_path TEXT NOT NULL,
                description TEXT NOT NULL,
                embedding TEXT,
                trigger_phrase TEXT COLLATE NOCASE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            -- AI Cron Jobs: a generic scheduled WhatsApp hook. created_by is either a
            -- real client's contact_name or the OWNER_PSEUDO_CONTACT sentinel
            -- (services/cron_scheduler.py) since the owner isn't a row in `clients`.
            -- target is 'all_clients', 'self', 'group' (a specific subset - see
            -- target_group), or an exact client contact_name.
            CREATE TABLE IF NOT EXISTS cron_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_by TEXT NOT NULL,
                name TEXT NOT NULL,
                natural_language_schedule TEXT NOT NULL,
                cron_expression TEXT NOT NULL,
                target TEXT NOT NULL,
                target_group TEXT,
                instructions TEXT NOT NULL,
                expects_response INTEGER NOT NULL DEFAULT 0,
                response_window_hours INTEGER,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                last_run_at TEXT,
                next_run_at TEXT
            );

            -- Generic key-value hook store a job reads from and writes to - a new use
            -- case never needs a new table, just a new key convention.
            CREATE TABLE IF NOT EXISTS job_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                contact_name TEXT,
                created_at TEXT NOT NULL
            );

            -- Ephemeral dispatch state: "is the next message from this contact an
            -- answer to a job we sent." Separate from job_data (durable content).
            -- Multiple rows per contact allowed - a contact can have more than one
            -- job awaiting a reply at once (e.g. a journal prompt and a broadcast
            -- landing close together); contact_name alone used to be the primary
            -- key, which silently clobbered an older pending job whenever a newer
            -- one was sent to the same contact - confirmed live, a journal prompt's
            -- pending row was overwritten by a broadcast two minutes later, orphaning
            -- the journal reply with nowhere to go.
            CREATE TABLE IF NOT EXISTS pending_job_responses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contact_name TEXT NOT NULL,
                job_id INTEGER NOT NULL,
                prompted_at TEXT NOT NULL,
                expires_at TEXT,
                prompt_message_id TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_pending_job_responses_contact
                ON pending_job_responses(contact_name);
        """)

        # One-time upgrade from the old contact_name-primary-key schema (single
        # pending job per contact) to the multi-row schema above - CREATE TABLE IF
        # NOT EXISTS above is a no-op against an already-existing old-schema table.
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(pending_job_responses)").fetchall()}
        if 'id' not in existing_cols:
            conn.executescript("""
                ALTER TABLE pending_job_responses RENAME TO pending_job_responses_old;
                CREATE TABLE pending_job_responses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contact_name TEXT NOT NULL,
                    job_id INTEGER NOT NULL,
                    prompted_at TEXT NOT NULL,
                    expires_at TEXT,
                    prompt_message_id TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_pending_job_responses_contact
                    ON pending_job_responses(contact_name);
                INSERT INTO pending_job_responses (contact_name, job_id, prompted_at, expires_at)
                    SELECT contact_name, job_id, prompted_at, expires_at FROM pending_job_responses_old;
                DROP TABLE pending_job_responses_old;
            """)
            logger.info("📇 Migrated pending_job_responses to multi-row-per-contact schema")

        # One-time upgrade for an existing cron_jobs table predating the 'group'
        # target (a specific subset of clients, e.g. "just the people who replied
        # yes to this poll") - CREATE TABLE IF NOT EXISTS above is a no-op against
        # an already-existing table, so the new column needs adding explicitly.
        # Nullable and additive: every existing row (target != 'group') is
        # unaffected, no data migration needed beyond the column existing.
        existing_job_cols = {row[1] for row in conn.execute("PRAGMA table_info(cron_jobs)").fetchall()}
        if 'target_group' not in existing_job_cols:
            conn.execute("ALTER TABLE cron_jobs ADD COLUMN target_group TEXT")
            logger.info("📇 Added target_group column to cron_jobs")

        # Same additive-column pattern for routines predating semantic matching
        # (services/routine_store.py) - nullable, so a routine created before this
        # column existed just has no embedding until it's next created/updated,
        # rather than needing a data migration.
        if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='routines'").fetchone():
            existing_routine_cols = {row[1] for row in conn.execute("PRAGMA table_info(routines)").fetchall()}
            if 'embedding' not in existing_routine_cols:
                conn.execute("ALTER TABLE routines ADD COLUMN embedding TEXT")
                logger.info("📇 Added embedding column to routines")
            if 'trigger_phrase' not in existing_routine_cols:
                conn.execute("ALTER TABLE routines ADD COLUMN trigger_phrase TEXT COLLATE NOCASE")
                logger.info("📇 Added trigger_phrase column to routines")

        for agent_id, (name, kind, tools) in PIPELINE_AGENT_DEFAULTS.items():
            defaults = {'model': 'qwen3:8b-16k', 'temperature': 0.7, 'timeout': 60} if agent_id == 'llm' else {}
            conn.execute(
                "INSERT OR IGNORE INTO agent_configs (id, name, kind, enabled, model, temperature, timeout, tools) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (agent_id, name, kind, defaults.get('model'), defaults.get('temperature'),
                 defaults.get('timeout'), json.dumps(tools)),
            )

        for agent_id, (name, prompt) in REASONING_CYCLE_DEFAULTS.items():
            conn.execute(
                "INSERT OR IGNORE INTO agent_configs "
                "(id, name, kind, enabled, model, temperature, timeout, prompt_template, tools) "
                "VALUES (?, ?, 'llm_stage', 0, 'qwen3:8b-16k', 0.7, 60, ?, '[]')",
                (agent_id, name, prompt),
            )

        has_any_persona = conn.execute("SELECT 1 FROM personas LIMIT 1").fetchone()
        if not has_any_persona:
            conn.execute(
                "INSERT INTO personas (id, name, system_prompt, active) VALUES (?, ?, ?, 1)",
                (DEFAULT_PERSONA_ID, DEFAULT_PERSONA_NAME, DEFAULT_PERSONA_PROMPT),
            )

        conn.commit()
    logger.info(f"✅ Database ready at {DB_PATH}")


def has_migrated_from_files() -> bool:
    """Whether migrate_from_files has ever completed. Must gate every call site -
    without this, re-running migration on a later startup would blindly UPDATE
    agent_configs/settings back to whatever's in the (possibly stale) old files,
    clobbering any change made since through the db (dashboard, WhatsApp admin, etc).
    Migration is a one-time import, not a sync."""
    return get_setting('_migrated_from_files', False)


def migrate_from_files(pipeline_json: Optional[dict], personas_json: Optional[dict], whitelist_names: List[str]):
    """One-time import from the old flat-file stores. Callers MUST check
    has_migrated_from_files() first - see docstring there."""
    with _connect() as conn:
        if pipeline_json:
            for agent_id, cfg in pipeline_json.get('agents', {}).items():
                row = conn.execute("SELECT id FROM agent_configs WHERE id = ?", (agent_id,)).fetchone()
                if not row:
                    continue
                conn.execute(
                    "UPDATE agent_configs SET enabled=?, model=COALESCE(?, model), "
                    "temperature=COALESCE(?, temperature), timeout=COALESCE(?, timeout), tools=? "
                    "WHERE id=?",
                    (1 if cfg.get('enabled', True) else 0, cfg.get('model'), cfg.get('temperature'),
                     cfg.get('timeout'), json.dumps(cfg.get('tools', [])), agent_id),
                )
            for key in ('ollama_url', 'qdrant_url', 'rabbitmq_url', 'whitelist_enabled', 'whitelist_prefix',
                        'max_response_length', 'openwa_url', 'openwa_api_key', 'openwa_session_name',
                        'openwa_poll_interval_seconds'):
                if key in pipeline_json:
                    conn.execute(
                        "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                        (key, json.dumps(pipeline_json[key])),
                    )

        if personas_json:
            active = personas_json.get('active_persona')
            for pid, p in personas_json.get('personas', {}).items():
                conn.execute(
                    "INSERT OR IGNORE INTO personas (id, name, system_prompt, active) VALUES (?, ?, ?, ?)",
                    (pid, p.get('name', pid), p.get('system_prompt', ''), 1 if pid == active else 0),
                )

        for name in whitelist_names:
            conn.execute(
                "INSERT OR IGNORE INTO clients (contact_name, registered_at, is_whitelisted) VALUES (?, ?, 1)",
                (name, _now()),
            )
        conn.commit()

    set_setting('_migrated_from_files', True)


def _now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S')


# ---------- agent_configs ----------

def _row_to_agent_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        'id': row['id'],
        'name': row['name'],
        'kind': row['kind'],
        'enabled': bool(row['enabled']),
        'model': row['model'],
        'temperature': row['temperature'],
        'timeout': row['timeout'],
        'prompt_template': row['prompt_template'],
        'tools': json.loads(row['tools']),
        'retry_count': row['retry_count'],
        'custom_params': json.loads(row['custom_params']),
    }


def get_agent_config(agent_id: str) -> Optional[Dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM agent_configs WHERE id = ?", (agent_id,)).fetchone()
        return _row_to_agent_dict(row) if row else None


def get_all_agent_configs() -> Dict[str, Dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM agent_configs").fetchall()
        return {row['id']: _row_to_agent_dict(row) for row in rows}


def update_agent_config(agent_id: str, **fields) -> bool:
    """Update any subset of: enabled, model, temperature, timeout, prompt_template, tools."""
    if not fields:
        return False
    allowed = {'enabled', 'model', 'temperature', 'timeout', 'prompt_template', 'tools', 'retry_count'}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    if 'tools' in updates:
        updates['tools'] = json.dumps(updates['tools'])
    if 'enabled' in updates:
        updates['enabled'] = 1 if updates['enabled'] else 0

    with _connect() as conn:
        exists = conn.execute("SELECT 1 FROM agent_configs WHERE id = ?", (agent_id,)).fetchone()
        if not exists:
            return False
        set_clause = ', '.join(f"{k} = ?" for k in updates)
        conn.execute(f"UPDATE agent_configs SET {set_clause} WHERE id = ?", (*updates.values(), agent_id))
        conn.commit()
        return True


def set_agent_enabled(agent_id: str, enabled: bool) -> bool:
    return update_agent_config(agent_id, enabled=enabled)


# ---------- personas ----------

def get_personas() -> Dict[str, Dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM personas").fetchall()
        return {row['id']: {'name': row['name'], 'system_prompt': row['system_prompt'],
                             'active': bool(row['active'])} for row in rows}


def get_active_persona_id() -> Optional[str]:
    with _connect() as conn:
        row = conn.execute("SELECT id FROM personas WHERE active = 1 LIMIT 1").fetchone()
        return row['id'] if row else None


def upsert_persona(persona_id: str, name: str, system_prompt: str):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO personas (id, name, system_prompt, active) VALUES (?, ?, ?, 0) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, system_prompt=excluded.system_prompt",
            (persona_id, name, system_prompt),
        )
        conn.commit()


def set_active_persona(persona_id: str) -> bool:
    with _connect() as conn:
        exists = conn.execute("SELECT 1 FROM personas WHERE id = ?", (persona_id,)).fetchone()
        if not exists:
            return False
        conn.execute("UPDATE personas SET active = 0")
        conn.execute("UPDATE personas SET active = 1 WHERE id = ?", (persona_id,))
        conn.commit()
        return True


def delete_persona(persona_id: str) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM personas WHERE id = ?", (persona_id,))
        conn.commit()
        return cur.rowcount > 0


# ---------- clients (replaces whitelist.txt) ----------

def is_whitelisted(contact_name: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT is_whitelisted FROM clients WHERE contact_name = ?", (contact_name,)
        ).fetchone()
        return bool(row and row['is_whitelisted'])


def add_client(contact_name: str, phone: str = None, lid: str = None) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO clients (contact_name, phone, lid, registered_at, is_whitelisted) "
            "VALUES (?, ?, ?, ?, 1) "
            "ON CONFLICT(contact_name) DO UPDATE SET is_whitelisted=1, "
            "phone=COALESCE(excluded.phone, clients.phone), lid=COALESCE(excluded.lid, clients.lid)",
            (contact_name, phone, lid, _now()),
        )
        conn.commit()


def remove_client(contact_name: str) -> bool:
    with _connect() as conn:
        cur = conn.execute("UPDATE clients SET is_whitelisted = 0 WHERE contact_name = ?", (contact_name,))
        conn.commit()
        return cur.rowcount > 0


def touch_last_active(contact_name: str):
    with _connect() as conn:
        conn.execute("UPDATE clients SET last_active_at = ? WHERE contact_name = ?", (_now(), contact_name))
        conn.commit()


def get_client(contact_name: str) -> Optional[Dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM clients WHERE contact_name = ?", (contact_name,)).fetchone()
        return _row_to_client_dict(row) if row else None


def list_clients(active_only: bool = False) -> List[Dict[str, Any]]:
    with _connect() as conn:
        query = "SELECT * FROM clients"
        if active_only:
            query += " WHERE is_whitelisted = 1"
        rows = conn.execute(query + " ORDER BY registered_at DESC").fetchall()
        return [_row_to_client_dict(row) for row in rows]


def update_client(contact_name: str, **fields) -> bool:
    allowed = {'phone', 'lid', 'notes', 'tags', 'is_whitelisted'}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    if 'tags' in updates:
        updates['tags'] = json.dumps(updates['tags'])
    if 'is_whitelisted' in updates:
        updates['is_whitelisted'] = 1 if updates['is_whitelisted'] else 0

    with _connect() as conn:
        exists = conn.execute("SELECT 1 FROM clients WHERE contact_name = ?", (contact_name,)).fetchone()
        if not exists:
            return False
        set_clause = ', '.join(f"{k} = ?" for k in updates)
        conn.execute(f"UPDATE clients SET {set_clause} WHERE contact_name = ?", (*updates.values(), contact_name))
        conn.commit()
        return True


def _row_to_client_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        'contact_name': row['contact_name'],
        'lid': row['lid'],
        'phone': row['phone'],
        'registered_at': row['registered_at'],
        'last_active_at': row['last_active_at'],
        'is_whitelisted': bool(row['is_whitelisted']),
        'notes': row['notes'],
        'tags': json.loads(row['tags']),
    }


# ---------- kb_documents ----------

def add_kb_document(filename: str, chunk_count: int, source: str = 'whatsapp_self_chat'):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO kb_documents (filename, ingested_at, chunk_count, source) VALUES (?, ?, ?, ?)",
            (filename, _now(), chunk_count, source),
        )
        conn.commit()


def get_kb_stats() -> Dict[str, Any]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as doc_count, COALESCE(SUM(chunk_count), 0) as total_chunks FROM kb_documents"
        ).fetchone()
        return {'documents_processed': row['doc_count'], 'total_chunks': row['total_chunks']}


def get_platform_stats(active_days: int = 7) -> Dict[str, Any]:
    with _connect() as conn:
        total = conn.execute("SELECT COUNT(*) as c FROM clients WHERE is_whitelisted = 1").fetchone()['c']
        cutoff = time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(time.time() - active_days * 86400))
        active = conn.execute(
            "SELECT COUNT(*) as c FROM clients WHERE is_whitelisted = 1 AND last_active_at >= ?", (cutoff,)
        ).fetchone()['c']
        active_names = [
            r['contact_name'] for r in conn.execute(
                "SELECT contact_name FROM clients WHERE is_whitelisted = 1 AND last_active_at >= ? "
                "ORDER BY last_active_at DESC", (cutoff,)
            ).fetchall()
        ]
    kb = get_kb_stats()
    return {
        'total_clients': total,
        'active_clients': active,
        'active_client_names': active_names,
        'active_window_days': active_days,
        **kb,
    }


# ---------- cron_jobs / job_data / pending_job_responses ----------

def _row_to_job_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        'id': row['id'],
        'created_by': row['created_by'],
        'name': row['name'],
        'natural_language_schedule': row['natural_language_schedule'],
        'cron_expression': row['cron_expression'],
        'target': row['target'],
        'target_group': json.loads(row['target_group']) if row['target_group'] else None,
        'instructions': row['instructions'],
        'expects_response': bool(row['expects_response']),
        'response_window_hours': row['response_window_hours'],
        'enabled': bool(row['enabled']),
        'created_at': row['created_at'],
        'last_run_at': row['last_run_at'],
        'next_run_at': row['next_run_at'],
    }


def create_cron_job(created_by: str, name: str, natural_language_schedule: str, cron_expression: str,
                     target: str, instructions: str, expects_response: bool = False,
                     response_window_hours: Optional[int] = None, next_run_at: Optional[str] = None,
                     target_group: Optional[List[str]] = None) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO cron_jobs (created_by, name, natural_language_schedule, cron_expression, target, "
            "target_group, instructions, expects_response, response_window_hours, enabled, created_at, "
            "next_run_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (created_by, name, natural_language_schedule, cron_expression, target,
             json.dumps(target_group) if target_group else None, instructions,
             1 if expects_response else 0, response_window_hours, _now(), next_run_at),
        )
        conn.commit()
        return cur.lastrowid


def get_cron_job(job_id: int) -> Optional[Dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM cron_jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_job_dict(row) if row else None


def list_cron_jobs(created_by: Optional[str] = None) -> List[Dict[str, Any]]:
    with _connect() as conn:
        if created_by:
            rows = conn.execute(
                "SELECT * FROM cron_jobs WHERE created_by = ? ORDER BY created_at DESC", (created_by,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM cron_jobs ORDER BY created_at DESC").fetchall()
        return [_row_to_job_dict(row) for row in rows]


def get_due_cron_jobs() -> List[Dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM cron_jobs WHERE enabled = 1 AND next_run_at IS NOT NULL AND next_run_at <= ?",
            (_now(),),
        ).fetchall()
        return [_row_to_job_dict(row) for row in rows]


def count_active_jobs_by_creator(created_by: str) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as c FROM cron_jobs WHERE created_by = ? AND enabled = 1", (created_by,)
        ).fetchone()
        return row['c']


def update_cron_job(job_id: int, **fields) -> bool:
    allowed = {'enabled', 'cron_expression', 'natural_language_schedule', 'instructions', 'target',
               'target_group', 'expects_response', 'response_window_hours', 'last_run_at', 'next_run_at'}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    if 'enabled' in updates:
        updates['enabled'] = 1 if updates['enabled'] else 0
    if 'expects_response' in updates:
        updates['expects_response'] = 1 if updates['expects_response'] else 0
    if 'target_group' in updates:
        updates['target_group'] = json.dumps(updates['target_group']) if updates['target_group'] else None

    with _connect() as conn:
        exists = conn.execute("SELECT 1 FROM cron_jobs WHERE id = ?", (job_id,)).fetchone()
        if not exists:
            return False
        set_clause = ', '.join(f"{k} = ?" for k in updates)
        conn.execute(f"UPDATE cron_jobs SET {set_clause} WHERE id = ?", (*updates.values(), job_id))
        conn.commit()
        return True


def delete_cron_job(job_id: int) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM cron_jobs WHERE id = ?", (job_id,))
        conn.commit()
        return cur.rowcount > 0


# ---------- routines (see services/routine_store.py) ----------

def upsert_routine(name: str, file_path: str, description: str, embedding: Optional[List[float]] = None,
                    trigger_phrase: Optional[str] = None, clear_trigger_phrase: bool = False):
    """embedding and trigger_phrase are both preserve-by-default: a caller that
    only wants to update one field (e.g. set_routine_trigger only changing
    trigger_phrase, or create_job_record re-saving a routine with no idea a
    trigger was ever configured) must never silently wipe the other via a bare
    None. Passing None keeps whatever's already stored (COALESCE); pass an
    explicit value to actually change it. clear_trigger_phrase=True is the one
    exception, needed because COALESCE alone can't distinguish "don't touch
    this" from "set it to NULL"."""
    now = _now()
    embedding_json = json.dumps(embedding) if embedding is not None else None
    effective_trigger_phrase = None if clear_trigger_phrase else trigger_phrase
    with _connect() as conn:
        conn.execute(
            "INSERT INTO routines (name, file_path, description, embedding, trigger_phrase, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET file_path=excluded.file_path, "
            "description=excluded.description, "
            "embedding=COALESCE(excluded.embedding, routines.embedding), "
            "trigger_phrase=CASE WHEN ? THEN NULL ELSE COALESCE(excluded.trigger_phrase, routines.trigger_phrase) END, "
            "updated_at=excluded.updated_at",
            (name, file_path, description, embedding_json, effective_trigger_phrase, now, now,
             1 if clear_trigger_phrase else 0),
        )
        conn.commit()


def get_routine_by_trigger_phrase(phrase: str) -> Optional[Dict[str, Any]]:
    """Fast, indexed-column lookup - called on every owner self-chat message, so
    this must not need reading any routine file. trigger_phrase's COLLATE NOCASE
    on the column makes this comparison case-insensitive."""
    with _connect() as conn:
        row = conn.execute("SELECT * FROM routines WHERE trigger_phrase = ?", (phrase,)).fetchone()
        return _row_to_routine_dict(row) if row else None


def _row_to_routine_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    d['embedding'] = json.loads(d['embedding']) if d.get('embedding') else None
    return d


def get_routine(name: str) -> Optional[Dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM routines WHERE name = ?", (name,)).fetchone()
        return _row_to_routine_dict(row) if row else None


def list_routines() -> List[Dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM routines ORDER BY name").fetchall()
        return [_row_to_routine_dict(r) for r in rows]


def delete_routine(name: str) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM routines WHERE name = ?", (name,))
        conn.commit()
        return cur.rowcount > 0


def write_job_data(job_id: int, key: str, value: str, contact_name: Optional[str] = None):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO job_data (job_id, key, value, contact_name, created_at) VALUES (?, ?, ?, ?, ?)",
            (job_id, key, value, contact_name, _now()),
        )
        conn.commit()


def read_job_data(job_id: int, key: Optional[str] = None, contact_name: Optional[str] = None) -> List[Dict[str, Any]]:
    with _connect() as conn:
        query = "SELECT * FROM job_data WHERE job_id = ?"
        params: List[Any] = [job_id]
        if key:
            query += " AND key = ?"
            params.append(key)
        if contact_name:
            query += " AND contact_name = ?"
            params.append(contact_name)
        rows = conn.execute(query + " ORDER BY created_at DESC", params).fetchall()
        return [
            {'key': r['key'], 'value': r['value'], 'contact_name': r['contact_name'], 'created_at': r['created_at']}
            for r in rows
        ]


def set_pending_job_response(contact_name: str, job_id: int, expires_at: Optional[str] = None,
                              prompt_message_id: Optional[str] = None):
    """A plain INSERT, not an upsert - a contact can have more than one job
    awaiting a reply at once (see the table's own comment in init_db). Each call
    adds a new row rather than replacing whatever was already pending."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO pending_job_responses (contact_name, job_id, prompted_at, expires_at, prompt_message_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (contact_name, job_id, _now(), expires_at, prompt_message_id),
        )
        conn.commit()


def get_pending_job_response(contact_name: str) -> Optional[Dict[str, Any]]:
    """Returns the MOST RECENTLY prompted still-pending job for this contact (a
    temporal correlation fallback - WhatsApp's own reply-to/quoted-message
    reference isn't exposed by this OpenWA build, so there's no stronger signal
    available to tell which of several outstanding prompts a reply is answering).
    Any expired rows encountered for this contact are cleaned up along the way -
    an expired pending marker must not keep intercepting messages, and older
    still-valid pending jobs are left untouched for a later reply to match."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM pending_job_responses WHERE contact_name = ? ORDER BY id DESC", (contact_name,)
        ).fetchall()
        if not rows:
            return None
        now = _now()
        expired_ids = [r['id'] for r in rows if r['expires_at'] and r['expires_at'] < now]
        if expired_ids:
            conn.executemany("DELETE FROM pending_job_responses WHERE id = ?", [(i,) for i in expired_ids])
            conn.commit()
        for row in rows:
            if row['id'] in expired_ids:
                continue
            return {
                'id': row['id'], 'contact_name': row['contact_name'], 'job_id': row['job_id'],
                'prompted_at': row['prompted_at'], 'expires_at': row['expires_at'],
                'prompt_message_id': row['prompt_message_id'],
            }
        return None


def clear_pending_job_response(pending_id: int):
    """Takes the specific row's id (from get_pending_job_response's 'id' field),
    not a contact_name - clearing by contact_name would also drop any OTHER still-
    pending jobs for that same contact, which is exactly the clobbering bug this
    schema exists to avoid."""
    with _connect() as conn:
        conn.execute("DELETE FROM pending_job_responses WHERE id = ?", (pending_id,))
        conn.commit()


# ---------- settings ----------

def get_setting(key: str, default: Any = None) -> Any:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row['value']) if row else default


def set_setting(key: str, value: Any):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )
        conn.commit()


def get_all_settings() -> Dict[str, Any]:
    with _connect() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {row['key']: json.loads(row['value']) for row in rows}
