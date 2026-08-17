"""
WhatsApp Admin Channel
The owner's self-chat isn't only for PDF uploads (services/kb_ingestion_poller.py) - a
plain text message there is treated as a natural-language admin request: agent config
("turn off momus"), client management ("add a client named Priya, number 9876543210"),
or platform stats ("how many active users this week").

Natural language in, but every mutation stays strict: the LLM's only job is to call a
tightly-typed tool (below) - never to freeform-edit the db. Each tool validates its own
inputs (agent id against the real 13, field against the real allowed set, client name
against what's on file) before touching config/control_plane.py or config/database.py.

Wired in by services/kb_ingestion_poller.py's poll loop (not a second independent
poller) - both PDF uploads and admin text share one fetch of the self-chat per cycle,
so this doesn't add a second consumer of OpenWA's rate-limited API budget.

This is also the ONLY place core/mcp_tools.py's load_owner_mcp_tools() (Gmail,
Calendar) may ever be bound. This channel is reachable only by the platform
owner's own WhatsApp self-chat (services/kb_ingestion_poller.py's chatId check),
never by a client - that boundary is what makes it safe to give this one agent
read access to the owner's personal email and calendar. Never pass owner_mcp_tools
(or anything read from it) into agents/llm_agent.py, agents/reasoning_cycle.py, or
services/cron_scheduler.py's job composer - those are reachable from a client's
own coaching conversation.
"""
import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

import config.database as db
from core.memory_index import get_memory_index

logger = logging.getLogger('OwnerAdminHandler')

# Same file agents/storage_agent.py writes to - read-only from here, never written.
INTERACTION_HISTORY_FILE = Path.home() / '.Adiyan' / 'interaction_history.jsonl'

# Appended to every reply this handler sends into the self-chat, and checked for on
# every INCOMING message before processing (see kb_ingestion_poller.py's _handle_message).
# Self-chat messages are always direction=outgoing (fromMe=true) whether they're the
# owner's own typed input or the bot's own reply - there is no other field that tells
# them apart. Without this tag, every reply the bot sends gets picked up on the next
# poll cycle and misread as a brand new command, which produced a real runaway
# self-conversation loop in testing (confirmed live - the bot kept re-answering its
# own previous answers with slightly reworded text).
ADMIN_REPLY_TAG = '[AdminAI]'

# 5 exchanges (user + reply pairs) of running context, bounded so the prompt doesn't
# grow unbounded over a long admin session.
MAX_HISTORY_MESSAGES = 10

# 60s was too tight for compound requests ("enable all the agents", "trigger this
# job now") that need several sequential tool-calls, each its own full round-trip
# to a local model - confirmed live, both timed out at 60s. 180s matches LLMAgent's
# own default timeout (agents/llm_agent.py) for the same class of local-model call;
# nothing here is blocking an interactive UI, so the extra headroom costs nothing.
ADMIN_AGENT_TIMEOUT_SECONDS = 180

ALL_AGENT_IDS = {
    'parser', 'validator', 'router', 'llm', 'synthesizer', 'storage', 'publisher',
    'hermes', 'prometheus', 'pythia', 'hephaestus', 'calliope', 'momus',
}
AGENT_CONFIG_FIELDS = {'enabled', 'model', 'temperature', 'timeout', 'prompt_template'}
CLIENT_FIELDS = {'notes', 'phone', 'tags'}

ADMIN_SYSTEM_PROMPT = (
    "You are Adiyan's admin assistant, used only by the platform owner through their own "
    "WhatsApp self-chat - not a client-facing conversation. Use the tools to look things up "
    "or make changes; never guess at data you haven't fetched. When asked for an overview "
    "or status of multiple agents, call list_agent_configs (one call, all 13 agents) rather "
    "than describing what you'd need to check or calling get_agent_config repeatedly - and "
    "when asked to enable/disable several agents at once (e.g. \"turn on all the reasoning "
    "stages\"), call update_agent_config once per agent in the same turn, not one at a time "
    "across replies. Keep your final reply short and factual, no coaching tone, no filler - a "
    "plain confirmation or a compact fact. If a request doesn't map to a real agent, field, or "
    "client, say so plainly rather than guessing or inventing one.\n\n"
    "You have READ-ONLY access to client conversation history via get_recent_client_messages "
    "and search_client_messages. Use it only to answer the owner's direct questions about what "
    "was said - report back facts (quote or summarize what was actually said), never invent "
    "content, and never give coaching advice yourself based on it. There is no tool to edit or "
    "delete a client's conversation history, by design.\n\n"
    "You can also schedule recurring WhatsApp jobs (create_job/list_jobs/enable_job/delete_job) - "
    "e.g. a weekly broadcast to all clients, or a nightly prompt-and-log routine. Confirm back "
    "the parsed schedule in plain terms (e.g. \"Scheduled for Sundays at 6:00 PM\") so the owner "
    "can catch a misparse immediately. Use trigger_job_now to send a job immediately for testing - "
    "it does not change the job's real scheduled time.\n\n"
    "For a ONE-TIME broadcast or note (the owner wants it sent once, not on a recurring "
    "schedule - e.g. \"send this to everyone this week\", \"a one time note asking...\"): "
    "use broadcast_once, not create_job - it sends immediately and never repeats, in one "
    "step. Use create_job only for something the owner actually wants recurring (\"every "
    "Sunday\", \"every night\"). IMPORTANT: create_job and broadcast_once's instructions are "
    "not carried out by you - when the job actually runs, a SEPARATE step composes the message "
    "and that step has its own web search/page-reading tools, even though you yourself don't. "
    "So a request like \"every morning, look up X and summarize it for everyone\" is still a "
    "plain create_job call - never refuse a scheduling/broadcast request just because look-up "
    "or research is involved; that happens later, at send time, not now. If the owner wants to review what people replied (e.g. "
    "before a call), use get_job_responses - never invent, guess, or give an example of "
    "what someone might have replied. A job that was just sent has no replies yet; say so "
    "plainly rather than fabricating one.\n\n"
    "IMPORTANT - targeting specific people: if a request names or implies a specific subset "
    "of clients (\"the people who said yes\", \"those who enrolled\", \"everyone who replied to "
    "job 21\"), that is target='group' with those exact names in target_group - NEVER "
    "target='all_clients' with wording like \"if you're part of X...\" in the instructions "
    "hoping the message self-filters. That still reaches every client, including people it "
    "has nothing to do with. Figure out who actually qualifies first (get_job_responses to "
    "read real replies, list_clients for the full roster), then pass exactly those names. If "
    "asked how many people a job reaches, answer from list_clients/get_platform_stats or the "
    "job's own target_group - that's always knowable, never say you can't determine it.\n\n"
    "If Gmail/Calendar tools are bound (they won't be until the owner completes one-time "
    "Google OAuth setup), use them to check the owner's own inbox or calendar when asked "
    "(e.g. \"what's on my calendar today\", \"did I get an email from X\") - these are "
    "read-only, so never claim to have sent an email or created/moved a calendar event; if "
    "asked to do either, say that capability isn't enabled. If no Gmail/Calendar tools are "
    "available at all, say so plainly rather than guessing at what might be on the calendar.\n\n"
    "If start_google_auth returns an authorization URL, your reply MUST include that URL "
    "copied character-for-character from the tool output - every query parameter, in the "
    "original order, no line break inserted inside it. Do not shorten it, drop parameters "
    "you don't recognize, describe it instead of pasting it, or reformat it in any way: "
    "Google rejects the link if even one parameter (like redirect_uri) is missing or altered. "
    "Paste the URL first, then add at most one short sentence around it."
)


GOOGLE_AUTH_URL_PATTERN = re.compile(r'https://accounts\.google\.com/o/oauth2/auth\?\S+')


def _extract_google_auth_url(tool_message: ToolMessage) -> Optional[str]:
    """Pulls the full authorization URL out of start_google_auth's raw tool output.
    Small local models reliably paraphrase/truncate long query strings when composing
    their own final reply (confirmed live: a real auth attempt failed at Google's own
    consent page with "Missing required parameter: redirect_uri" even though the tool's
    raw output - verified directly against the running workspace-mcp process - had a
    complete, correct URL). Content can be a plain string or workspace-mcp's list-of-
    content-block form ([{'type': 'text', 'text': ...}]) depending on the MCP transport,
    so both are checked."""
    content = tool_message.content
    if isinstance(content, list):
        content = " ".join(
            block.get('text', '') for block in content if isinstance(block, dict)
        )
    if not isinstance(content, str):
        return None
    match = GOOGLE_AUTH_URL_PATTERN.search(content)
    return match.group(0) if match else None


def _build_admin_tools(control_plane, model_name: str, ollama_url: str, cron_scheduler=None,
                        owner_mcp_tool_count: int = 0) -> List:
    @tool
    def list_agent_configs() -> list:
        """List every agent's enabled/model/temperature/timeout in one call - the 7
        pipeline agents (parser, validator, router, llm, synthesizer, storage,
        publisher) plus the 6 LLM reasoning-cycle stages (hermes, prometheus,
        pythia, hephaestus, calliope, momus). Use this instead of calling
        get_agent_config repeatedly when the owner asks for an overview or to
        enable/disable several agents at once."""
        return [
            {'id': agent_id, 'name': cfg.name, 'enabled': cfg.enabled, 'model': cfg.model,
             'temperature': cfg.temperature, 'timeout': cfg.timeout}
            for agent_id in sorted(ALL_AGENT_IDS)
            for cfg in [control_plane.get_agent_config(agent_id)] if cfg
        ]

    @tool
    def get_agent_config(agent_id: str) -> dict:
        """Get one agent's current config: enabled, model, temperature, timeout. agent_id
        must be one of: parser, validator, router, llm, synthesizer, storage, publisher,
        hermes, prometheus, pythia, hephaestus, calliope, momus."""
        if agent_id not in ALL_AGENT_IDS:
            return {'error': f"Unknown agent '{agent_id}'. Valid: {sorted(ALL_AGENT_IDS)}"}
        cfg = control_plane.get_agent_config(agent_id)
        if not cfg:
            return {'error': f"Agent '{agent_id}' has no config"}
        return {
            'id': agent_id, 'name': cfg.name, 'enabled': cfg.enabled,
            'model': cfg.model, 'temperature': cfg.temperature, 'timeout': cfg.timeout,
        }

    @tool
    def update_agent_config(agent_id: str, field: str, value: str) -> dict:
        """Update one field of an agent's config. field must be one of: enabled, model,
        temperature, timeout, prompt_template. For 'enabled' pass 'true'/'false'. Applies
        immediately, live - no restart needed."""
        if agent_id not in ALL_AGENT_IDS:
            return {'error': f"Unknown agent '{agent_id}'. Valid: {sorted(ALL_AGENT_IDS)}"}
        if field not in AGENT_CONFIG_FIELDS:
            return {'error': f"Unknown field '{field}'. Valid: {sorted(AGENT_CONFIG_FIELDS)}"}

        if field == 'enabled':
            parsed = value.strip().lower() in ('true', 'on', 'yes', '1')
        elif field == 'temperature':
            try:
                parsed = float(value)
            except ValueError:
                return {'error': f"temperature must be a number, got {value!r}"}
        elif field == 'timeout':
            try:
                parsed = int(value)
            except ValueError:
                return {'error': f"timeout must be an integer, got {value!r}"}
        else:
            parsed = value

        ok = control_plane.update_agent_config(agent_id, **{field: parsed})
        return {'success': ok, 'agent_id': agent_id, 'field': field, 'value': parsed}

    @tool
    def get_client(name: str) -> dict:
        """Look up a registered client's details by contact name."""
        client = db.get_client(name)
        return client or {'error': f"No client named '{name}'"}

    @tool
    def list_clients(only_active: bool = False) -> list:
        """List registered clients. only_active=true limits to clients active in the last
        7 days."""
        return db.list_clients(active_only=only_active)

    @tool
    async def add_client(name: str, phone: str = '') -> dict:
        """Register a new client (coach-initiated onboarding, no self-registration
        message needed). phone is optional, but without it Adiyan can't proactively
        message this client (scheduled jobs, broadcasts) until they message in
        first - a normal reply always carries its own chat id."""
        if not name or not name.strip():
            return {'error': 'name is required'}
        phone = phone.strip() or None
        lid = None
        if phone and cron_scheduler:
            try:
                lid = await cron_scheduler.openwa.resolve_chat_id(phone)
            except Exception as e:
                logger.warning(f"⚠️  Could not resolve chat id for new client '{name}' from phone: {e}")
        db.add_client(name.strip(), phone=phone, lid=lid)
        result = {'success': True, 'name': name.strip()}
        if phone and not lid:
            result['warning'] = (
                "Phone number saved, but couldn't resolve a WhatsApp chat id for it yet - "
                "scheduled messages to this client won't go through until they message Adiyan "
                "at least once, or the number is confirmed reachable on WhatsApp."
            )
        return result

    @tool
    def update_client(name: str, field: str, value: str) -> dict:
        """Update a registered client's notes, phone, or tags. field must be one of:
        notes, phone, tags."""
        if field not in CLIENT_FIELDS:
            return {'error': f"Unknown field '{field}'. Valid: {sorted(CLIENT_FIELDS)}"}
        ok = db.update_client(name, **{field: value})
        return {'success': ok, 'name': name} if ok else {'error': f"No client named '{name}'"}

    @tool
    def remove_client(name: str) -> dict:
        """Unregister/remove a client by name."""
        ok = db.remove_client(name)
        return {'success': ok} if ok else {'error': f"No client named '{name}'"}

    @tool
    def get_platform_stats() -> dict:
        """Platform stats: total registered clients, clients active in the last 7 days
        (and their names), documents processed into the knowledge base, total chunks."""
        return db.get_platform_stats()

    @tool
    def get_recent_client_messages(name: str, limit: int = 5) -> list:
        """Read-only: the most recent messages exchanged with a client, newest first -
        each with timestamp, what they said, and what the coach (Adiyan) replied. For
        "what's my most recent conversation with X" / "show me X's last few messages".
        Use search_client_messages instead for "did X ever mention Y" style questions."""
        limit = max(1, min(limit, 20))
        matches = []
        try:
            with open(INTERACTION_HISTORY_FILE, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if record.get('contact_name', '').lower() == name.lower():
                        matches.append(record)
        except FileNotFoundError:
            return {'error': 'No interaction history recorded yet'}

        if not matches:
            return {'error': f"No message history for '{name}'"}
        matches.sort(key=lambda r: r.get('timestamp', ''), reverse=True)
        return [
            {'timestamp': r.get('timestamp'), 'message': r.get('message'), 'response': r.get('response')}
            for r in matches[:limit]
        ]

    @tool
    def search_client_messages(name: str, query: str, limit: int = 5) -> list:
        """Read-only: semantic search over a specific client's conversation history for
        something they may have discussed - e.g. "did X ever mention their income" or
        "what has X said about their goals". Returns the most relevant past exchanges,
        not necessarily the most recent - use get_recent_client_messages for that."""
        memory = get_memory_index(control_plane.config.qdrant_url, control_plane.config.ollama_url)
        if not memory:
            return {'error': 'Memory index unavailable'}
        limit = max(1, min(limit, 10))
        results = memory.retrieve(query, contact_name=name, top_k=limit)
        return results or {'error': f"No relevant history found for '{name}'"}

    @tool
    async def create_job(name: str, natural_language_schedule: str, target: str, instructions: str,
                          expects_response: bool = False, response_window_hours: int = 0,
                          target_group: Optional[List[str]] = None,
                          description: Optional[str] = None) -> dict:
        """Create a scheduled WhatsApp job (AI Cron Job). target must be 'all_clients',
        'self' (your own self-chat), 'group' (a specific subset of clients - pass their
        exact names in target_group, e.g. ["Sripriya", "Kumar"]), or an exact client name.
        Use 'group' whenever the request is about specific people rather than everyone -
        e.g. "the people who replied yes to job 21": call get_job_responses(21) first,
        read who actually said yes, then pass exactly those names in target_group. Never
        use 'all_clients' as a stand-in for a subset and rely on the message wording to
        self-filter - that reaches everyone, not just the people who qualify.
        natural_language_schedule is free text like 'every Sunday at 6pm' or
        'every night at 9' - it's parsed into a real schedule automatically.
        instructions describes what the message should say (and may reference the
        knowledge base - the job composer can search it). Set expects_response=true
        and response_window_hours (0 = no expiry) to capture whatever the
        recipient(s) reply with next as data, instead of normal coaching. description
        is a short one-line summary stored in the routines index (list_routines) to
        help you and future you recognize this job by what it does, not just its name.

        IMPORTANT: every job name is also a routine name (list_routines). If this
        exact name already exists, this does NOT create a duplicate - it triggers
        the existing routine right now instead and tells you it reused it. Check
        list_routines first if you're unsure whether something like this already
        exists before picking a name."""
        from services.cron_scheduler import OWNER_PSEUDO_CONTACT
        if not cron_scheduler:
            return {'error': 'Cron scheduler is not available yet (still starting up)'}
        if target not in ('all_clients', 'self', 'group') and not db.get_client(target):
            return {'error': f"No client named '{target}'. Use 'all_clients', 'self', 'group', or an exact client name."}
        try:
            result = await cron_scheduler.create_or_trigger(
                created_by=OWNER_PSEUDO_CONTACT, name=name, natural_language_schedule=natural_language_schedule,
                target=target, instructions=instructions, expects_response=expects_response,
                response_window_hours=response_window_hours or None,
                model_name=model_name, ollama_url=ollama_url, target_group=target_group,
                description=description,
            )
        except ValueError as e:
            return {'error': str(e)}
        return {'success': True, **result}

    @tool
    def list_routines() -> list:
        """List every known routine (name + description) - the durable, reusable
        library every job creates an entry in, independent of whether it's
        currently active/scheduled. create_job already matches new requests
        against this library semantically (by meaning, not just exact name), so
        you don't strictly need to check it first - but it's useful for browsing
        what already exists."""
        return [{k: v for k, v in r.items() if k != 'embedding'} for r in db.list_routines()]

    @tool
    def delete_routine(routine_name: str) -> dict:
        """Permanently delete a routine definition (its index entry and file) -
        does not touch any currently scheduled job using it. Use delete_job for
        that separately if it's also actively scheduled."""
        from services.routine_store import delete_routine_file, routine_file_path
        ok = db.delete_routine(routine_name)
        if not ok:
            return {'error': f"No routine named '{routine_name}'"}
        delete_routine_file(routine_file_path(routine_name))
        return {'success': True}

    @tool
    def list_jobs() -> list:
        """List all scheduled jobs - both owner-created and every client's own
        self-service reminders."""
        return db.list_cron_jobs()

    @tool
    def enable_job(job: str, enabled: bool) -> dict:
        """Enable or disable a scheduled job (does not delete it) - pass either its
        id or its exact name (e.g. "daily_stock_report" or 20)."""
        from services.cron_scheduler import resolve_job
        found, error = resolve_job(job, ollama_url=ollama_url)
        if error:
            return {'error': error}
        db.update_cron_job(found['id'], enabled=enabled)
        return {'success': True}

    @tool
    def delete_job(job: str) -> dict:
        """Permanently delete a scheduled job - pass either its id or its exact name."""
        from services.cron_scheduler import resolve_job
        found, error = resolve_job(job, ollama_url=ollama_url)
        if error:
            return {'error': error}
        db.delete_cron_job(found['id'])
        return {'success': True}

    @tool
    async def broadcast_once(name: str, target: str, instructions: str,
                              expects_response: bool = False, response_window_hours: int = 0,
                              target_group: Optional[List[str]] = None) -> dict:
        """Send a ONE-TIME message right now - for a single announcement, ask, or
        reminder, NOT a recurring schedule. target must be 'all_clients', 'self',
        'group' (a specific subset - pass their exact names in target_group), or an
        exact client name. Same 'group' guidance as create_job: use it for anything
        aimed at specific people rather than everyone. Creates, sends, and permanently
        disables the underlying job in one step - nothing to clean up afterward, and
        it will never fire again on its own. Set expects_response=true to capture
        replies (read them back later with get_job_responses)."""
        from services.cron_scheduler import OWNER_PSEUDO_CONTACT
        if not cron_scheduler:
            return {'error': 'Cron scheduler is not available yet (still starting up)'}
        if target not in ('all_clients', 'self', 'group') and not db.get_client(target):
            return {'error': f"No client named '{target}'. Use 'all_clients', 'self', 'group', or an exact client name."}
        try:
            result = await cron_scheduler.broadcast_once(
                created_by=OWNER_PSEUDO_CONTACT, name=name, target=target, instructions=instructions,
                expects_response=expects_response, response_window_hours=response_window_hours or None,
                target_group=target_group,
            )
        except ValueError as e:
            return {'error': str(e)}
        return {'success': True, **result}

    @tool
    def get_job_responses(job: str) -> dict:
        """Read back what recipients have replied to a job so far (its collected
        job_data) - e.g. broadcast replies or journal entries, useful for reviewing
        before a call. Pass either the job's id or its exact name. Each response
        includes who sent it and when."""
        from services.cron_scheduler import resolve_job
        found, error = resolve_job(job, ollama_url=ollama_url)
        if error:
            return {'error': error}
        responses = db.read_job_data(found['id'])
        if not responses:
            return {'job_name': found['name'], 'responses': [], 'note': 'No responses collected yet'}
        return {'job_name': found['name'], 'responses': responses}

    @tool
    async def trigger_job_now(job: str) -> dict:
        """Manually run a scheduled job right now, for testing - composes and sends
        its message immediately without waiting for its actual scheduled time. Pass
        either the job's id or its exact name. Does NOT change that scheduled time
        (the job's real next run is unaffected - this is a test send, not a
        reschedule)."""
        if not cron_scheduler:
            return {'error': 'Cron scheduler is not available yet (still starting up)'}
        from services.cron_scheduler import resolve_job
        found, error = resolve_job(job, ollama_url=ollama_url)
        if error:
            return {'error': error}
        return await cron_scheduler.run_now(found)

    @tool
    def check_google_workspace_status() -> dict:
        """Check whether Gmail/Calendar tools are set up and working - use this
        when the owner asks things like "is my calendar connected" or "why can't
        you see my email"."""
        from core.mcp_tools import is_google_workspace_configured
        if not is_google_workspace_configured():
            return {
                'status': 'not_configured',
                'message': 'Google Workspace credentials have not been set up yet - run '
                           'tools/set_secret.py GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET '
                           'to store them in the vault, then restart Adiyan.',
            }
        if owner_mcp_tool_count == 0:
            return {
                'status': 'configured_but_unavailable',
                'message': 'Credentials are stored but no Gmail/Calendar tools loaded - either the '
                           'one-time Google sign-in consent was never completed, or Adiyan needs a '
                           'restart to pick up the credentials.',
            }
        return {
            'status': 'connected',
            'tool_count': owner_mcp_tool_count,
            'message': f'Gmail/Calendar are connected ({owner_mcp_tool_count} tools available, read-only).',
        }

    return [list_agent_configs, get_agent_config, update_agent_config, get_client, list_clients,
            add_client, update_client, remove_client, get_platform_stats,
            get_recent_client_messages, search_client_messages,
            create_job, list_jobs, enable_job, delete_job, trigger_job_now, get_job_responses,
            list_routines, delete_routine,
            check_google_workspace_status,
            broadcast_once]


class OwnerAdminHandler:
    """One instance per process, shared by kb_ingestion_poller.py for every non-document
    self-chat message."""

    def __init__(self, control_plane, openwa_service, ollama_url: str = None, cron_scheduler=None,
                 owner_mcp_tools: Optional[List] = None):
        self.control_plane = control_plane
        self.openwa = openwa_service
        self.ollama_url = ollama_url or control_plane.config.ollama_url
        # Lets trigger_job_now reuse CronScheduler's own send logic (run_now())
        # instead of duplicating it - None is fine (the tool just reports
        # "not available yet") since main.py constructs the scheduler in the same
        # step as this handler and always passes it through.
        self.cron_scheduler = cron_scheduler
        # Owner-only MCP tools (Gmail, Calendar - core/mcp_tools.py's
        # load_owner_mcp_tools()). Empty list, never None, if unconfigured - see
        # this module's own docstring for why this must never be the
        # client-facing pool.
        self.owner_mcp_tools = owner_mcp_tools or []
        # Reuses the 'llm' agent's configured model rather than inventing a separate
        # admin-specific one - one less thing to independently configure.
        llm_cfg = control_plane.get_agent_config('llm')
        self.model = llm_cfg.model if llm_cfg and llm_cfg.model else 'qwen3:8b-16k'
        # Running context for the admin conversation - "what's hermes' temperature" ->
        # "now set it to 0.3" needs to know "it" means hermes. In-memory only, one
        # instance per process (main.py constructs this once), bounded so an old
        # session doesn't grow the prompt forever. Separate from the per-client
        # conversation memory in core/memory_index.py - this is the owner's own admin
        # channel, never mixed with client-facing coaching context.
        self._history: List = []

    async def handle_text_message(self, chat_id: str, message_body: str) -> Optional[str]:
        """Returns the sent reply's own message id (or None on send failure) - the
        caller (KBIngestionPoller) must mark this id as already-processed immediately,
        or its own reply lands back in the self-chat on the next poll and gets
        misread as a new command (self-chat messages are always direction=outgoing,
        same as everything else here - there's no way to tell "the bot's own reply"
        from "a new user message" except by tracking sent ids ourselves)."""
        if not message_body or not message_body.strip():
            return None
        try:
            reply = await self._run_admin_agent(message_body)
        except Exception as e:
            logger.error(f"❌ Admin request failed: {e}", exc_info=True)
            if isinstance(e, TimeoutError):
                # str(TimeoutError()) is '' - without this, the owner sees the
                # literally-empty "Couldn't process that: " and has no idea why.
                # Could be this call's own timeout, or a tool it invoked timing out
                # internally (e.g. a job's message composer) - both raise the same
                # plain TimeoutError, so this can't name a specific number reliably.
                detail = (
                    "took too long to complete - it may need several steps, or the "
                    "system is under heavy load right now; try again in a moment or "
                    "a narrower request"
                )
            else:
                detail = str(e) or type(e).__name__
            reply = f"Couldn't process that: {detail}"

        try:
            result = await self.openwa.send_message(chat_id, f"{reply}\n\n{ADMIN_REPLY_TAG}")
            return result.get('messageId')
        except Exception as e:
            logger.error(f"❌ Failed to send admin reply: {e}")
            return None

    async def _run_admin_agent(self, message_body: str) -> str:
        from langchain_ollama import ChatOllama
        from langgraph.prebuilt import create_react_agent

        model = ChatOllama(model=self.model, base_url=self.ollama_url, temperature=0.2)
        tools = _build_admin_tools(self.control_plane, self.model, self.ollama_url, self.cron_scheduler,
                                    owner_mcp_tool_count=len(self.owner_mcp_tools))
        tools = tools + self.owner_mcp_tools
        agent = create_react_agent(model, tools)

        human = HumanMessage(content=message_body)
        # The model has no built-in clock - confirmed live it otherwise guesses a
        # training-data-era date (e.g. queried a calendar range in 2023) for any
        # relative request like "next week". Stamped fresh per call, not once at
        # startup, since the admin process can stay up across real calendar days.
        current_date_notice = f"Today's date is {datetime.now().strftime('%A, %Y-%m-%d')}."
        system = SystemMessage(content=f"{ADMIN_SYSTEM_PROMPT}\n\n{current_date_notice}")
        messages = [system] + self._history + [human]
        result = await asyncio.wait_for(agent.ainvoke({"messages": messages}), timeout=ADMIN_AGENT_TIMEOUT_SECONDS)

        final = result["messages"][-1]
        if not isinstance(final, AIMessage) or not final.content:
            raise Exception("Admin agent produced no final answer")

        reply = final.content.strip()

        # Don't trust the LLM to relay a Google auth URL byte-for-byte - see
        # _extract_google_auth_url's docstring for the confirmed live failure this
        # guards against. If start_google_auth was called this turn, force the exact
        # tool-returned URL into the reply rather than whatever the model composed.
        for msg in result["messages"]:
            if isinstance(msg, ToolMessage) and msg.name == 'start_google_auth':
                auth_url = _extract_google_auth_url(msg)
                if auth_url and auth_url not in reply:
                    reply = (
                        "Open this link to authorize Google access:\n"
                        f"{auth_url}\n\n"
                        "After approving, retry your request."
                    )
                break

        # Only the user's turn and the final answer are kept - not the intermediate
        # tool-call/tool-result messages the react loop produced getting there, which
        # would otherwise pollute future turns with stale tool-call artifacts.
        self._history.extend([human, AIMessage(content=reply)])
        self._history = self._history[-MAX_HISTORY_MESSAGES:]
        return reply
