"""
AI Cron Jobs — a generic scheduled WhatsApp hook engine.

A job (config/database.py's `cron_jobs` table) pairs a schedule (natural language,
parsed once via services/schedule_parser.py into a real cron expression) with a
hook: compose one WhatsApp message - LLM, with the same full MCP tool access every
agents/reasoning_cycle.py stage already gets, plus read-only access to this job's
own stored data - and send it to the job's target. If the job expects a response,
the target's next reply is captured generically into job_data instead of being
routed through normal coaching (the intercept lives in agents/validator_agent.py
for clients, and in services/kb_ingestion_poller.py for the owner's own self-chat).

Poller shape mirrors services/openwa_poller.py / services/kb_ingestion_poller.py
exactly: async start()/stop(), joined into the same background thread in main.py.
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from croniter import croniter
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool

import config.database as db
from services.openwa_service import OpenWAService
from services.schedule_parser import parse_natural_schedule

logger = logging.getLogger('CronScheduler')

# Sentinel contact_name for the owner's own self-chat, so pending_job_responses /
# job_data / cron_jobs.created_by can key an owner-authored job the same way a
# client-authored one is keyed - the owner isn't a row in the `clients` table.
OWNER_PSEUDO_CONTACT = '__owner__'

TICK_INTERVAL_SECONDS = 60.0

# 60s was too tight under real load - confirmed live, a broadcast_once composer
# call timed out competing with the main orchestrator's own Ollama calls on the
# same local model. Nothing here blocks an interactive UI (jobs run in the
# background either way), so the extra headroom costs nothing.
JOB_COMPOSER_TIMEOUT_SECONDS = 120

# Per-creator cap for non-owner (client self-service) job creation - a sane ceiling
# against runaway creation, not full rate-limiting infra. The owner has no cap.
CLIENT_JOB_CAP = 5

JOB_COMPOSER_SYSTEM_PROMPT = (
    "You compose one WhatsApp message for a scheduled job, following the "
    "instructions below. This exact text is sent as-is to every recipient - there "
    "is no per-recipient personalization step, so never use a placeholder like "
    "[Name], [Date], or similar that won't actually get filled in; write it "
    "generically (e.g. \"Hey there\" or no name at all) instead. Use the "
    "read_job_data tool if the instructions reference previously stored content "
    "(e.g. \"today's lesson\", past responses). Respond with ONLY the message "
    "text to send - no preamble, no quotes around it, nothing else."
)

# Appended only when the job expects a reply (journal prompts, broadcasts that
# collect responses). Confirmed live and necessary: given only a vague instruction
# like "Track your daily journal entry", the composer wrote out a fabricated,
# first-person example journal entry ("Reflecting on accomplishments...") and SENT
# THAT to the recipient as the message itself - not a question asking them to
# journal, an invented answer standing in for one. Without this, "the notification"
# and "a fabricated response" become indistinguishable.
JOB_COMPOSER_EXPECTS_RESPONSE_ADDENDUM = (
    "\n\nThis message is a QUESTION or REQUEST you are asking the recipient - you "
    "are not answering it yourself. Never write as if you already have their "
    "answer, and never invent an example, sample, or illustration of what a good "
    "answer might look like - that reads as a real answer and gets confused for "
    "one. End the message with a clear, direct ask for what you want them to reply "
    "with."
)


def _now() -> datetime:
    return datetime.now()


def _fmt(dt: datetime) -> str:
    return dt.strftime('%Y-%m-%dT%H:%M:%S')


def resolve_job(identifier: str, created_by: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Resolves a job by either its numeric id or its exact name (case-insensitive) -
    every job-management tool below takes this instead of a bare job_id, so a
    conversation can refer to "the daily_stock_report job" instead of tracking
    numeric ids across turns. Confirmed live as a real problem: the admin agent
    repeatedly mixed up which job ids 19/20/21/22 actually referred to mid-
    conversation. If created_by is given, only that contact's own jobs are
    considered (client self-service scoping) - a client can never resolve or act
    on another client's job by guessing its name, even if they know it.

    Returns (job, None) on a clean match, or (None, error) - including, when a
    name matches more than one job, an error listing every match's id so the
    caller can fall back to specifying by id."""
    identifier = (identifier or '').strip()
    if not identifier:
        return None, "No job id or name given"

    if identifier.isdigit():
        job = db.get_cron_job(int(identifier))
        if job and (created_by is None or job['created_by'] == created_by):
            return job, None
        return None, f"No job with id {identifier}"

    candidates = db.list_cron_jobs(created_by=created_by) if created_by else db.list_cron_jobs()
    matches = [j for j in candidates if j['name'].lower() == identifier.lower()]
    if not matches:
        return None, f"No job named '{identifier}'"
    if len(matches) > 1:
        ids = ', '.join(f"id {m['id']}" for m in matches)
        return None, f"Multiple jobs are named '{identifier}' - specify by id instead ({ids})"
    return matches[0], None


async def create_job_record(
    *, created_by: str, name: str, natural_language_schedule: str, target: str,
    instructions: str, expects_response: bool, response_window_hours: Optional[int],
    model_name: str, ollama_url: str, cap: Optional[int] = None,
    target_group: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Shared by both authoring paths (owner admin tools, client self-service
    tools) - one validated write path, never a freeform db insert. Raises
    ValueError on a bad schedule, an exceeded cap, or (target='group') an empty
    or unresolvable target_group - each surfaces back to whoever asked as a
    plain error rather than silently creating a job that sends to no one or
    everyone. target_group is only meaningful when target='group'; ignored
    otherwise (the caller-facing tool validates this pairing up front)."""
    if cap is not None:
        active = db.count_active_jobs_by_creator(created_by)
        if active >= cap:
            raise ValueError(
                f"You already have {active} active job(s) (limit {cap}) - "
                "disable or cancel one first."
            )

    if target == 'group':
        if not target_group:
            raise ValueError("target='group' requires a non-empty target_group list of exact client names.")
        unknown = [n for n in target_group if not db.get_client(n)]
        if unknown:
            raise ValueError(f"Unknown client name(s) in target_group: {', '.join(unknown)}")

    cron_expression = await parse_natural_schedule(natural_language_schedule, model_name, ollama_url)
    next_run = croniter(cron_expression, _now()).get_next(datetime)
    job_id = db.create_cron_job(
        created_by=created_by, name=name, natural_language_schedule=natural_language_schedule,
        cron_expression=cron_expression, target=target, instructions=instructions,
        expects_response=expects_response, response_window_hours=response_window_hours,
        next_run_at=_fmt(next_run), target_group=target_group if target == 'group' else None,
    )
    return db.get_cron_job(job_id)


# A one-time job (services/owner_admin_handler.py's broadcast_once tool) is
# created, sent, and disabled in the same call - its cron_expression is never
# actually evaluated, so there's no need to burn an LLM call parsing a real
# schedule from it (and "once, right now" isn't a schedule the parser could
# meaningfully turn into a cron expression anyway). A fixed placeholder is fine.
ONE_TIME_PLACEHOLDER_CRON = '0 0 1 1 *'


def build_client_job_tools(contact_name: str, model_name: str, ollama_url: str) -> List:
    """Self-service scheduling tools for a specific client, closed over their own
    contact_name - target is hardcoded to 'self' and created_by to this contact at
    the Python level, never parameters the LLM supplies, so no phrasing (including
    a deliberate injection attempt like "schedule a job targeting client X") can
    make a client's job address anyone but themselves."""

    @tool
    async def create_my_job(name: str, natural_language_schedule: str, instructions: str,
                             expects_response: bool = True, response_window_hours: int = 0) -> dict:
        """Schedule a personal reminder for yourself - Adiyan will message you at the
        time you describe (e.g. 'every night at 9', 'every Monday morning'). If
        expects_response is true, whatever you reply next is saved rather than
        treated as a normal coaching question. response_window_hours=0 means no
        expiry (waits indefinitely for your reply)."""
        try:
            job = await create_job_record(
                created_by=contact_name, name=name, natural_language_schedule=natural_language_schedule,
                target='self', instructions=instructions, expects_response=expects_response,
                response_window_hours=response_window_hours or None,
                model_name=model_name, ollama_url=ollama_url, cap=CLIENT_JOB_CAP,
            )
        except ValueError as e:
            return {'error': str(e)}
        return {
            'success': True, 'job_id': job['id'], 'cron_expression': job['cron_expression'],
            'next_run_at': job['next_run_at'],
        }

    @tool
    def list_my_jobs() -> list:
        """List your own scheduled reminders."""
        return db.list_cron_jobs(created_by=contact_name)

    @tool
    def cancel_my_job(job: str) -> dict:
        """Cancel (permanently delete) one of your own scheduled reminders - pass
        either its id or its exact name. Cannot cancel a job that isn't yours."""
        found, error = resolve_job(job, created_by=contact_name)
        if error:
            return {'error': error}
        db.delete_cron_job(found['id'])
        return {'success': True}

    return [create_my_job, list_my_jobs, cancel_my_job]


def _build_job_data_tool(job_id: int):
    @tool
    def read_job_data(key: str = '') -> list:
        """Read this job's previously stored data. Pass a key to filter (e.g.
        'broadcast_content'), or leave blank to see everything stored so far."""
        return db.read_job_data(job_id, key=key or None)
    return read_job_data


async def _compose_message(job: Dict[str, Any], mcp_tools: List, model_name: str, ollama_url: str) -> str:
    """One tool-capable LLM call, bound to the full MCP tool set (identical to what
    every agents/reasoning_cycle.py stage gets) plus a read-only view of this job's
    own data. Sending is deliberately not a tool available here - dispatch below is
    always deterministic Python, never an LLM decision."""
    from langchain_ollama import ChatOllama
    from langgraph.prebuilt import create_react_agent

    model = ChatOllama(model=model_name, base_url=ollama_url, temperature=0.5)
    tools = list(mcp_tools) + [_build_job_data_tool(job['id'])]
    agent = create_react_agent(model, tools)

    system_prompt = JOB_COMPOSER_SYSTEM_PROMPT
    if job.get('expects_response'):
        system_prompt += JOB_COMPOSER_EXPECTS_RESPONSE_ADDENDUM

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=job['instructions']),
    ]
    result = await asyncio.wait_for(agent.ainvoke({"messages": messages}), timeout=JOB_COMPOSER_TIMEOUT_SECONDS)
    final = result["messages"][-1]
    if not isinstance(final, AIMessage) or not final.content:
        raise Exception("Job composer produced no final message")
    return final.content.strip()


async def _resolve_chat_id_for_client(openwa: OpenWAService, client: Dict[str, Any]) -> Optional[str]:
    """A client's lid is normally captured live off their first message
    (agents/parser_agent.py's registration flow) - but a client registered before
    that capture existed, or added via the admin channel's add_client with only a
    phone number, has no lid on file. Falls back to resolving it from their phone
    through OpenWA, and caches the result onto the client record so this is a
    one-time cost, not a per-job lookup. Returns None if there's neither a stored
    lid nor a phone to resolve from."""
    if client.get('lid'):
        return client['lid']
    if not client.get('phone'):
        return None
    try:
        resolved = await openwa.resolve_chat_id(client['phone'])
    except Exception as e:
        logger.warning(f"⚠️  Could not resolve chat id for {client['contact_name']} from phone: {e}")
        return None
    if resolved:
        db.update_client(client['contact_name'], lid=resolved)
        logger.info(f"📇 Resolved and cached chat id for {client['contact_name']} from phone")
    return resolved


async def _resolve_targets(job: Dict[str, Any], openwa: OpenWAService) -> List[Dict[str, Optional[str]]]:
    """Returns [{'contact_name', 'chat_id'}]. chat_id may be None (no lid or
    resolvable phone on file, or an owner-'self' job before the caller resolves the
    owner's own chat id) - callers skip unresolvable targets rather than failing
    the whole job."""
    target = job['target']
    if target == 'all_clients':
        return [
            {'contact_name': c['contact_name'], 'chat_id': await _resolve_chat_id_for_client(openwa, c)}
            for c in db.list_clients(active_only=True)
        ]
    if target == 'group':
        # A specific subset (e.g. "just the people who replied yes to a poll"),
        # not every client - see create_job_record's target_group param. Names
        # not matching a real client are silently dropped rather than failing the
        # whole job: create_job already validates every name up front, so a
        # missing one here means the client was removed after the job was made.
        results = []
        for contact_name in (job.get('target_group') or []):
            client = db.get_client(contact_name)
            if not client:
                continue
            results.append({
                'contact_name': contact_name,
                'chat_id': await _resolve_chat_id_for_client(openwa, client),
            })
        return results
    if target == 'self':
        creator = job['created_by']
        if creator == OWNER_PSEUDO_CONTACT:
            return [{'contact_name': OWNER_PSEUDO_CONTACT, 'chat_id': None}]
        client = db.get_client(creator)
        chat_id = await _resolve_chat_id_for_client(openwa, client) if client else None
        return [{'contact_name': creator, 'chat_id': chat_id}]
    client = db.get_client(target)
    chat_id = await _resolve_chat_id_for_client(openwa, client) if client else None
    return [{'contact_name': target, 'chat_id': chat_id}]


class CronScheduler:
    """Same async start()/stop() shape as OpenWAPoller/KBIngestionPoller, ticking
    every ~60s (cron's own granularity floor - no point polling faster)."""

    def __init__(self, openwa_service: OpenWAService, mcp_tools: List,
                 model_name: str, ollama_url: str, tick_interval_seconds: float = TICK_INTERVAL_SECONDS):
        self.openwa = openwa_service
        self.mcp_tools = mcp_tools
        self.model_name = model_name
        self.ollama_url = ollama_url
        self.tick_interval_seconds = tick_interval_seconds
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._owner_chat_id: Optional[str] = None

    async def start(self):
        if self._running:
            logger.warning("Cron scheduler already running")
            return
        self._running = True
        self._task = asyncio.create_task(self._tick_loop())
        logger.info(f"🚀 Cron scheduler started (interval={self.tick_interval_seconds}s)")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("🛑 Cron scheduler stopped")

    async def _tick_loop(self):
        while self._running:
            try:
                await self._tick_once()
            except Exception as e:
                logger.error(f"❌ Cron tick failed: {e}", exc_info=True)
            await asyncio.sleep(self.tick_interval_seconds)

    async def _tick_once(self):
        for job in db.get_due_cron_jobs():
            try:
                await self._run_job(job)
            except Exception as e:
                logger.error(f"❌ Job '{job['name']}' (id={job['id']}) failed: {e}", exc_info=True)
                # Advance next_run_at even on failure - a permanently-broken job
                # (bad instructions, unreachable target) must not spin every tick.
                self._advance(job)

    async def _resolve_owner_chat_id(self) -> Optional[str]:
        if not self._owner_chat_id:
            self._owner_chat_id = await self.openwa.get_own_chat_id()
        return self._owner_chat_id

    async def _run_job(self, job: Dict[str, Any]):
        """The real scheduled path: send, then advance last_run_at/next_run_at."""
        await self.run_now(job)
        self._advance(job)

    async def run_now(self, job: Dict[str, Any]) -> Dict[str, Any]:
        """Compose and send a job's message immediately, exactly as the scheduler
        would. Deliberately does NOT touch last_run_at/next_run_at - shared between
        the real scheduled tick (_run_job, which advances the schedule right after
        calling this) and the owner admin channel's manual "trigger this job now to
        test it" tool, which must NOT consume or shift the job's real next
        occurrence just because the owner wanted to see what it sends."""
        if job['created_by'] == OWNER_PSEUDO_CONTACT and job['target'] == 'self':
            targets = [{'contact_name': OWNER_PSEUDO_CONTACT, 'chat_id': await self._resolve_owner_chat_id()}]
        else:
            targets = await _resolve_targets(job, self.openwa)

        resolvable = [t for t in targets if t['chat_id']]
        skipped = len(targets) - len(resolvable)
        if skipped:
            logger.warning(
                f"⚠️  Job '{job['name']}' (id={job['id']}): {skipped} target(s) with no known chat id, skipped"
            )
        if not resolvable:
            logger.warning(f"⚠️  Job '{job['name']}' (id={job['id']}) has no resolvable targets - nothing sent")
            return {'sent': 0, 'skipped': skipped, 'message': None}

        message = await _compose_message(job, self.mcp_tools, self.model_name, self.ollama_url)

        expires_at = None
        if job['response_window_hours']:
            expires_at = _fmt(_now() + timedelta(hours=job['response_window_hours']))

        sent = 0
        for t in resolvable:
            try:
                send_result = await self.openwa.send_message(t['chat_id'], message)
                sent += 1
                if job['expects_response']:
                    db.set_pending_job_response(
                        t['contact_name'], job['id'], expires_at=expires_at,
                        prompt_message_id=send_result.get('messageId'),
                    )
            except Exception as e:
                logger.error(f"❌ Failed to send job '{job['name']}' to {t['contact_name']}: {e}")

        logger.info(f"✅ Job '{job['name']}' (id={job['id']}) sent to {sent}/{len(resolvable)} target(s)")
        return {'sent': sent, 'skipped': skipped, 'message': message}

    async def broadcast_once(self, *, created_by: str, name: str, target: str, instructions: str,
                              expects_response: bool = False, response_window_hours: Optional[int] = None,
                              cap: Optional[int] = None, target_group: Optional[List[str]] = None) -> Dict[str, Any]:
        """Create, send, and permanently disable a job in one call - for a genuine
        one-off broadcast or note, not a recurring schedule.

        Deliberately a single atomic Python call rather than the owner admin agent
        chaining create_job -> trigger_job_now -> enable_job(false) itself: confirmed
        live that asking a local model to reason through 3 sequential tool calls
        blew well past even the raised 180s admin timeout with nothing to show for
        it. One tool call means one model decision (call broadcast_once) plus the
        one inherent LLM call inside run_now() that composes the actual message -
        the minimum possible, not whatever the ReAct loop happens to need."""
        if cap is not None:
            active = db.count_active_jobs_by_creator(created_by)
            if active >= cap:
                raise ValueError(
                    f"You already have {active} active job(s) (limit {cap}) - "
                    "disable or cancel one first."
                )
        if target == 'group':
            if not target_group:
                raise ValueError("target='group' requires a non-empty target_group list of exact client names.")
            unknown = [n for n in target_group if not db.get_client(n)]
            if unknown:
                raise ValueError(f"Unknown client name(s) in target_group: {', '.join(unknown)}")
        job_id = db.create_cron_job(
            created_by=created_by, name=name, natural_language_schedule='one-time, sent immediately',
            cron_expression=ONE_TIME_PLACEHOLDER_CRON, target=target, instructions=instructions,
            expects_response=expects_response, response_window_hours=response_window_hours,
            next_run_at=None, target_group=target_group if target == 'group' else None,
        )
        job = db.get_cron_job(job_id)
        result = await self.run_now(job)
        db.update_cron_job(job_id, enabled=False)
        return {'job_id': job_id, **result}

    def _advance(self, job: Dict[str, Any]):
        now = _now()
        next_run = croniter(job['cron_expression'], now).get_next(datetime)
        db.update_cron_job(job['id'], last_run_at=_fmt(now), next_run_at=_fmt(next_run))
