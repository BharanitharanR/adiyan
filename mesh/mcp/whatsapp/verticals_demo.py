"""
Backend for the Verticals marketing site's live demo
(https://bharanitharanr.github.io/verticals/): a visitor types one
paragraph describing their business into the site's hero text box, and this
turns it into a real, working Adiyan vertical they can message on WhatsApp
within seconds - the actual product, not a mockup of it.

Public and unauthenticated by necessity - a marketing-site visitor has no
Adiyan account or token yet. Safe to expose anyway because every vertical
this creates is unmistakably a demo (a 'demo-' vertical_id prefix and a
random suffix, never colliding with or resembling a real business's own
vertical_id like 'home-finder-hyd'), self-deactivates
_DEMO_TTL_SECONDS after creation regardless of what happens to the caller,
and creation is rate-limited per IP. No phone number is ever collected or
messaged here - the confirmation (WhatsApp number, summon phrase, spec PDF)
is handed back in the HTTP response for the page to display; the visitor
messages Adiyan themselves, on their own initiative, exactly the same way a
real customer would.

Reuses mesh/config_agent/skills/apply_vertical_spec.py's own validation and
write logic directly - a same-process Python import, not an A2A round trip,
since this server already lives in the same codebase - rather than
reimplementing any part of it. A demo vertical is written through the exact
same allowlisted, all-or-nothing path a real WhatsApp-uploaded spec goes
through, including its own workflows: handling
(apply_vertical_spec._generate_workflows) if the generated spec names any.

Expiry is a real cron_trigger registration (_schedule_expiry below), not an
in-memory asyncio timer - confirmed live this session that the earlier
asyncio.sleep()-based version died on every whatsapp_mcp restart, leaving a
demo vertical (demo-copper-kettle-cafe-73632f) stuck active for hours past
its intended 1-hour TTL. cron_trigger persists the job in Mongo and fires
it via a normal A2A call to config_agent's deactivate_vertical skill, so it
survives this server restarting.
"""
import asyncio
import base64
import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import yaml
from pydantic import BaseModel, Field

from mesh.config_agent.constants import AGENT_URL as CONFIG_AGENT_URL
from mesh.config_agent.skills import apply_vertical_spec
from mesh.lib.agent_sdk import AdiyanAgent
from mesh.lib.paths import mcp_home

logger = logging.getLogger('VerticalsDemo')

_agent = AdiyanAgent('verticals_demo')

_DEMO_TTL_SECONDS = 60 * 60  # 1 hour, per this session's explicit decision
_PDF_DIR = mcp_home('whatsapp') / 'verticals_demo_pdfs'
_PDF_DIR.mkdir(parents=True, exist_ok=True)

# In-memory, per-process sliding window - good enough for a demo endpoint;
# a restart just resets everyone's count early, never blocks anyone longer
# than intended.
_RATE_LIMIT_MAX_PER_IP = 5
_RATE_LIMIT_WINDOW_SECONDS = 3600
_rate_limit_hits: Dict[str, List[float]] = {}


def check_rate_limit(ip: str) -> bool:
    """True if `ip` may create another demo vertical right now. Also prunes
    that IP's own expired hits while checking, so this dict never grows
    without bound across a long-running process."""
    now = time.time()
    hits = [t for t in _rate_limit_hits.get(ip, []) if now - t < _RATE_LIMIT_WINDOW_SECONDS]
    if len(hits) >= _RATE_LIMIT_MAX_PER_IP:
        _rate_limit_hits[ip] = hits
        return False
    hits.append(now)
    _rate_limit_hits[ip] = hits
    return True


class GeneratedVerticalSpec(BaseModel):
    """Deliberately small, structured fragments - NOT one free-form
    paragraph for the model to format itself. Confirmed live this session:
    asking qwen3:8b-16k for a single "You are <phrase>, the digital
    assistant for..." paragraph in one shot got a summon_phrase that was
    literally the entire input description with spaces stripped, and a
    business_persona_context that was just the raw description restated in
    third person - neither followed the required framing at all. Breaking
    the ask into short, narrow fields (a tone phrase, a couple of do/don't
    instructions) and ASSEMBLING the actual persona paragraph in code
    (_assemble_persona below) guarantees the required framing regardless
    of what the model does with prose, while the model still supplies the
    real content."""
    business_name: str = Field(description="The business's real name, exactly as implied by the description - never invented or embellished.")
    summon_phrase: str = Field(description="ONE short lowercase word, 5-12 letters, no spaces - e.g. 'spicehouse' for a restaurant called Spice House, or 'crumbs' for a bakery called Sweet Crumbs. Never a sentence or the description itself.")
    card_description: str = Field(description="One sentence describing what this assistant does for this specific business.")
    tone: str = Field(description="2-4 words describing the tone to speak in, e.g. 'warm and friendly', 'brisk and professional'.")
    key_dos: List[str] = Field(
        default_factory=list,
        description=(
            "0-3 short instructions for things the assistant should always do, each a few words, e.g. "
            "'confirm pickup time clearly'. Empty list if the description gives nothing specific. NEVER an "
            "instruction to provide a menu, prices, inventory, stock levels, or any other specific fact - this "
            "demo never collects that data, so an instruction like 'always give menu and prices' guarantees "
            "the assistant will invent numbers it doesn't have the moment a customer asks. If the description "
            "only names what the business sells with no actual prices/inventory given, leave that detail out "
            "of key_dos entirely rather than turning it into an instruction to report it."
        ),
    )
    key_donts: List[str] = Field(default_factory=list, description="0-3 short instructions for things the assistant should never do, each a few words, e.g. 'quote exact prices, redirect to the owner instead'. Empty list if the description gives nothing specific.")
    workflows: List[str] = Field(
        default_factory=list,
        description=(
            "0-3 short plain-English sentences naming a real action customers should be able to take, ONLY if "
            "the description clearly implies one: booking/reserving a slot, placing an order, cancelling one, "
            "paying, or leaving feedback. Each entry MUST be a full sentence like 'Customers can book an "
            "appointment' - never a single word. An EMPTY list is the correct, expected answer for most "
            "businesses - do not invent a workflow that isn't clearly implied."
        ),
    )


_GENERATE_PROMPT_TEMPLATE = """A business owner described their business in their own words below. Extract the fields needed to configure a WhatsApp assistant for it.

Business owner's own words:
\"\"\"{description}\"\"\"

Example, for "We're a laundry pickup service in Bangalore called QuickWash, we collect and deliver same day":
- business_name: "QuickWash"
- summon_phrase: "quickwash"
- card_description: "A same-day laundry pickup and delivery service in Bangalore."
- tone: "efficient and friendly"
- key_dos: ["confirm pickup and delivery timing clearly"]
- key_donts: []
- workflows: ["Customers can book a laundry pickup"]

Ground every field ONLY in what the owner actually said - never invent a specific fact (a price, an hour, a policy) they didn't mention, and never invent a workflow that isn't clearly implied."""


async def _generate_spec(description: str) -> GeneratedVerticalSpec:
    prompt = _GENERATE_PROMPT_TEMPLATE.format(description=description)
    return await _agent.ask(
        prompt, stage='generate_vertical_spec', model='qwen3:8b-16k', temperature=0.3, schema=GeneratedVerticalSpec,
    )


# Confirmed live: the model wrote key_dos: ["Always provide menu and price
# information"] for a cafe description that never gave a menu or a single
# price - the demo has no document-upload step, so that instruction had no
# possible source to be grounded in. The very next real customer message
# asking for prices got six fabricated ones back, despite strict_grounding
# being on and this same persona's own closing line saying never to guess.
# A prompt instruction alone isn't reliable enough here (this incident is
# the proof) - dropped in code as a second, mandatory layer, not a
# suggestion the model can weigh against the business description.
#
# Confirmed live a second time on the SAME already-created vertical (before
# this filter existed): key_dos also included "Always process takeaway
# orders for pickup" with no real ordering workflow behind it, and the
# model used that instruction to fabricate a fake "order processed"
# confirmation on top of the fabricated price - an explicit "always
# process/confirm/complete X" persona command overrides the grounding rule
# just as reliably as an "always report X" one does. Real order/booking/
# payment handling belongs ONLY in the workflows: field below (which wires
# an actual tool), never as a bare key_dos assertion with nothing backing
# it.
_UNGROUNDABLE_DO_KEYWORDS = (
    'menu', 'price', 'pricing', 'cost', 'inventory', 'stock', 'catalog', 'catalogue',
    'process order', 'process the order', 'process takeaway', 'process pickup',
    'confirm order', 'confirm the order', 'complete order', 'complete payment',
    'process payment', 'mark order', 'order is processed', 'order processed',
)


def _assemble_persona(summon_phrase: str, generated: 'GeneratedVerticalSpec') -> str:
    """The one place that actually writes the "You are <phrase>..."
    instruction paragraph - see GeneratedVerticalSpec's own docstring for
    why this is assembled here instead of trusted from the model
    verbatim."""
    # Same defensive-fallback pattern as summon_phrase in _build_spec -
    # confirmed live the model can leave this blank, which would otherwise
    # silently produce "Speak in a  tone." (double space, no real tone).
    tone = generated.tone.strip() or 'warm and helpful'
    parts = [
        f"You are {summon_phrase}, the digital assistant for {generated.business_name}. "
        f"{generated.card_description} Speak in a {tone} tone.",
    ]
    safe_dos = [do for do in generated.key_dos if not any(kw in do.lower() for kw in _UNGROUNDABLE_DO_KEYWORDS)]
    for do in safe_dos[:3]:
        parts.append(f"Always {do.strip().rstrip('.')}.")
    for dont in generated.key_donts[:3]:
        parts.append(f"Never {dont.strip().rstrip('.')}.")
    parts.append("If you don't know something, say so honestly rather than guessing or inventing an answer.")
    return ' '.join(parts)


def _slugify(text: str) -> str:
    slug = re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')
    return slug or 'business'


def _build_spec(generated: GeneratedVerticalSpec) -> Dict[str, Any]:
    """(vertical_id, summon_phrase, spec dict ready for yaml.safe_dump).
    vertical_id always carries the 'demo-' prefix plus a random suffix -
    the isolation mechanism this session decided on, never reusable as (or
    confusable with) a real production vertical_id."""
    vertical_id = f"demo-{_slugify(generated.business_name)}-{uuid.uuid4().hex[:6]}"

    # Never trust the model's summon_phrase as-is - confirmed live it can
    # return the entire input description with punctuation stripped rather
    # than the one short word asked for. Falls back to a slug of the
    # business name (itself capped) whenever the model's answer is empty,
    # implausibly long, or too short to be a real word.
    summon_phrase = re.sub(r'[^a-z0-9]', '', generated.summon_phrase.lower())
    if not (3 <= len(summon_phrase) <= 20):
        summon_phrase = _slugify(generated.business_name).replace('-', '')[:20] or 'assistant'

    persona = _assemble_persona(summon_phrase, generated)
    # Same defensive filter as summon_phrase above - drop anything that
    # isn't a real multi-word sentence (confirmed live: the model returned
    # a bare 'default' here once, which would otherwise reach
    # apply_vertical_spec's own workflow classifier as noise).
    workflows = [w for w in generated.workflows if isinstance(w, str) and len(w.split()) >= 3][:3]
    spec: Dict[str, Any] = {
        'vertical_id': vertical_id,
        'business_name': generated.business_name,
        'agents': {
            'orchestrator': {
                'constants': {
                    'summon_phrase': summon_phrase,
                    'card_description': generated.card_description,
                    'business_persona_context': persona,
                },
            },
            'analysis': {
                'constants': {
                    'strict_grounding': True,
                    'business_persona_context': persona,
                },
            },
            'scheduler': {'constants': {'business_persona_context': persona}},
            'journal': {'constants': {'business_persona_context': persona}},
            'adiyan_reader': {'constants': {'business_persona_context': persona}},
            'config_agent': {'constants': {'business_persona_context': persona}},
        },
    }
    if workflows:
        spec['workflows'] = workflows
    return {'vertical_id': vertical_id, 'summon_phrase': summon_phrase, 'spec': spec}


def _write_pdf(vertical_id: str, yaml_text: str) -> Path:
    """Same "plain YAML, no titles" shape this session's own
    btwin_adiyan_config fix established - a title/prose header above the
    YAML broke pypdf/yaml.safe_load's own round trip once already; this
    PDF is generated fresh from the exact text just applied, so it can
    never drift from what's actually live."""
    from fpdf import FPDF

    pdf = FPDF(format='Letter')
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font('Courier', size=9)
    for line in yaml_text.split('\n'):
        pdf.cell(0, 4.6, line, new_x='LMARGIN', new_y='NEXT')
    path = _PDF_DIR / f'{vertical_id}.pdf'
    pdf.output(str(path))
    return path


async def _schedule_expiry(vertical_id: str) -> None:
    """Registers a durable one-shot cron_trigger fire that deactivates
    `vertical_id` after _DEMO_TTL_SECONDS, via config_agent's own
    deactivate_vertical skill. Replaces a prior asyncio.sleep()-based timer
    that lived only in this process's memory - confirmed live this session
    that a whatsapp_mcp restart (routine during normal use, let alone
    active debugging) silently killed that timer, leaving a demo vertical
    (demo-copper-kettle-cafe-73632f) stuck active for hours past its
    intended 1-hour TTL. cron_trigger persists jobs in Mongo and reloads
    them on its own startup, so this survives a whatsapp_mcp restart the
    old approach couldn't."""
    invoke_at = (datetime.now(timezone.utc) + timedelta(seconds=_DEMO_TTL_SECONDS)).isoformat()
    try:
        await _agent.schedule(
            job_id=f'demo-expiry-{vertical_id}',
            invoke_at=invoke_at,
            target_agent_url=CONFIG_AGENT_URL,
            skill_id='deactivate_vertical',
            params={'vertical_id': vertical_id},
        )
    except Exception as e:
        logger.warning(f'Failed to schedule expiry for demo vertical {vertical_id!r}: {e}')


async def register_demo_vertical(description: str) -> Dict[str, Any]:
    """{'ok': True, vertical_id, summon_phrase, business_name, pdf_path,
    expires_in_seconds} on success, {'ok': False, 'error': <user-facing
    message>} otherwise - never a raw exception, same "never leak an
    internal error to the person on the other end" rule this codebase's
    WhatsApp-facing skills already follow."""
    if not description or not description.strip():
        return {'ok': False, 'error': 'Tell us a bit about your business first.'}
    if len(description) > 2000:
        return {'ok': False, 'error': 'That description is too long - a sentence or two is plenty.'}

    try:
        generated = await _generate_spec(description.strip())
    except Exception as e:
        logger.error(f'Demo spec generation failed: {e}')
        return {'ok': False, 'error': "Couldn't understand that - try describing your business in a sentence or two."}

    built = _build_spec(generated)
    vertical_id, summon_phrase, spec = built['vertical_id'], built['summon_phrase'], built['spec']

    # A deactivated demo vertical's summon_phrase is never freed -
    # list_vertical_ids() (which apply_vertical_spec's own collision check
    # reads) lists every vertical with ANY config on file, active or not -
    # so two demo submissions landing on the same derived phrase (the same
    # business name tried twice, or just a coincidence) collide even
    # though the earlier one auto-expired hours ago. Confirmed live this
    # session. Retried with a numeric suffix rather than failing outright -
    # cheap and self-healing, since collisions should be rare.
    for attempt in range(4):
        yaml_text = yaml.safe_dump(spec, sort_keys=False, allow_unicode=True)
        content_b64 = base64.b64encode(yaml_text.encode('utf-8')).decode('ascii')
        try:
            result = await apply_vertical_spec.run(content_b64, 'website-demo.yaml')
        except Exception as e:
            logger.error(f'Demo vertical apply failed for {vertical_id!r}: {e}')
            return {'ok': False, 'error': 'Could not register this demo vertical right now - try again in a moment.'}

        if result.get('applied'):
            break

        error = result.get('error') or ''
        if 'already used by vertical' in error and attempt < 3:
            summon_phrase = f"{built['summon_phrase']}{attempt + 2}"
            vertical_id = f"{built['vertical_id']}-{attempt + 2}"
            spec['vertical_id'] = vertical_id
            spec['agents']['orchestrator']['constants']['summon_phrase'] = summon_phrase
            continue

        logger.warning(f'Demo vertical rejected for {vertical_id!r}: {error}')
        return {'ok': False, 'error': "Couldn't set that up - try describing your business a bit differently."}

    pdf_path = _write_pdf(vertical_id, yaml_text)
    await _schedule_expiry(vertical_id)

    return {
        'ok': True,
        'vertical_id': vertical_id,
        'summon_phrase': summon_phrase,
        'business_name': generated.business_name,
        'pdf_path': str(pdf_path),
        'expires_in_seconds': _DEMO_TTL_SECONDS,
    }


# --- async job wrapper -------------------------------------------------
# register_demo_vertical() above is correct but slow: local structured-
# output generation on qwen3:8b-16k plus (when the description implies
# one) a real n8n workflow create+activate round trip confirmed live this
# session to take anywhere from ~30s to ~100s+ depending on machine load.
# A single HTTP request held open that long is fragile on a real visitor's
# connection - confirmed live that a weak mobile connection killed the
# request outright partway through, surfacing as a generic network error
# on the site with no way to tell "still working" apart from "failed".
#
# Fixed by making the HTTP layer fire-and-poll instead of holding one long
# connection: POST /verticals/register (server.py) starts a job and
# returns its id in well under a second; the page then polls GET
# /verticals/status/{job_id} every few seconds - each poll is a fast,
# cheap request that a flaky connection can retry freely without losing
# the actual work in progress, which keeps running server-side regardless
# of whether any particular poll succeeds.
_JOBS: Dict[str, Dict[str, Any]] = {}
_JOB_TTL_SECONDS = 600  # how long a finished job's result stays pollable


def _prune_old_jobs() -> None:
    now = time.time()
    stale = [jid for jid, job in _JOBS.items() if now - job['created_at'] > _JOB_TTL_SECONDS]
    for jid in stale:
        del _JOBS[jid]


async def _run_job(job_id: str, description: str) -> None:
    try:
        result = await register_demo_vertical(description)
    except Exception as e:
        logger.error(f'Demo job {job_id!r} crashed: {e}')
        result = {'ok': False, 'error': 'Something went wrong building this - try again in a moment.'}
    if result.get('ok'):
        # Enriched here, once, at completion - not on every poll - so
        # get_session_status() (a real HTTP call to OpenWA) doesn't run
        # once per poll interval for however long the visitor keeps
        # checking back.
        from mesh.mcp.whatsapp.server import _openwa
        status = await _openwa.get_session_status()
        result['whatsapp_number'] = status.get('phone')
        result['pdf_url'] = f"/verticals/pdf/{result['vertical_id']}"
        del result['pdf_path']
    _JOBS[job_id]['status'] = 'done'
    _JOBS[job_id]['result'] = result


def start_registration_job(description: str) -> str:
    _prune_old_jobs()
    job_id = uuid.uuid4().hex
    _JOBS[job_id] = {'status': 'pending', 'result': None, 'created_at': time.time()}
    asyncio.create_task(_run_job(job_id, description))
    return job_id


def get_job(job_id: str) -> Dict[str, Any]:
    job = _JOBS.get(job_id)
    if job is None:
        return {'status': 'not_found'}
    return {'status': job['status'], 'result': job['result']}
