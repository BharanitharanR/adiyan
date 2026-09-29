"""
read_next_page's real body - fired nightly by cron_trigger (see
start_reading.py's initial registration, and this module's own
re-registration at the end of run()). Pulls the next unsent page,
synthesizes it to speech, sends it as a real WhatsApp voice note, generates
that page's comprehension questions and preloads them for the next
morning's dispatch_questions.py fire, advances current_page, and
re-registers itself for tomorrow night - mesh/scheduler/skills/
run_routine.py's own recurrence pattern, not cron_trigger holding a
recurring schedule itself.

Grounded only in the page's own real text, same "never invent" rule this
whole mesh already follows elsewhere (mesh/scheduler/skills/run_routine.py's
_compose_generic, mesh/analysis/skills/analyze.py's strict_grounding) - if
the book has run out of pages, that's said plainly and the reading job is
deactivated, not silently looped or papered over with invented content.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from croniter import croniter
from pydantic import BaseModel, Field

from mesh.adiyan_reader import db, eval_quality, tts
from mesh.lib.config import load_seed_config
from mesh.adiyan_reader.constants import (
    AGENT_ID, AGENT_URL, CRON_TRIGGER_URL, MEMORY_AGENT_URL, OLLAMA_URL,
)
from mesh.lib import config_sdk, permissions
from mesh.lib.agent_sdk import AdiyanAgent
from mesh.lib.mcp_client import call_tool
from mesh.lib.paths import state_db_path

AGENT_CODE_DIR = Path(__file__).parent.parent
_SEED = load_seed_config(AGENT_CODE_DIR)
# One instance, module-level - every method mints its own token internally
# against 'adiyan_reader_service' (mesh/lib/permissions_config.json).
# Replaces a direct OpenWAService() construction that bypassed
# whatsapp_mcp's permission check entirely - see the Developer Guide's
# pitfall section.
_agent = AdiyanAgent(AGENT_ID)


def _seeded(key: str) -> Dict[str, Any]:
    return _SEED.get(key, {'value': '', 'description': ''})


class ComprehensionQuestions(BaseModel):
    questions: List[str] = Field(description="Short comprehension/reflection questions about the page's actual content - nothing outside it.")


async def _generate_questions(page_text: str, cfg: Dict[str, Any], count: int) -> List[str]:
    seeded = _seeded('generate_questions_prompt_template')
    template = await config_sdk.get_constant(
        AGENT_ID, 'generate_questions_prompt_template', seeded['value'], description=seeded['description'],
    )
    fmt_kwargs = dict(page_text=page_text, count=count)
    try:
        prompt = template.format(**fmt_kwargs)
    except Exception:
        prompt = seeded['value'].format(**fmt_kwargs)
    result = await _agent.ask(
        prompt, stage='generate_questions', model=cfg['model'], temperature=cfg['temperature'], schema=ComprehensionQuestions,
    )
    return result.questions[:count]


async def _synthesize_and_send_page(
    job: Dict[str, Any], chat_id: str, page_number: int, clearer: bool = False,
) -> Optional[str]:
    """The shared "narrate exactly this one page" primitive - fetch,
    rewrite-if-not-prose, emotion-tag, synthesize, send text+voice. Returns
    the page's own real text on success, None if the page doesn't exist.

    Deliberately does NOT touch db.advance_page, question generation, or
    cron scheduling - those are policy decisions that differ between a
    single nightly/on-demand page (run(), below) and a multi-page burst
    (read_range.py): run() advances the bookmark and deactivates the job
    the moment a page comes back missing (the book is genuinely finished);
    read_range.py instead stops its own loop quietly and lets its caller
    decide how far the bookmark should move, since replaying an earlier
    range must never drag the ongoing nightly bookmark backward. Splitting
    this out is what lets both callers reuse the exact same fetch/TTS/send
    logic without duplicating it or accidentally sharing the wrong side
    effects."""
    page_result = await _agent.call_agent(MEMORY_AGENT_URL, 'get_book_page', {
        'source_filename': job['source_filename'], 'page_number': page_number,
    })
    if not page_result.get('found'):
        return None

    page_text = page_result['text']

    # speech_text is what actually gets narrated - page_text (the real page
    # content) stays untouched below for _generate_questions(), which needs
    # to stay grounded in what the page actually says, not a short spoken
    # rewrite of it.
    #
    # tts.rewrite_for_speech() runs on EVERY page now, not gated behind a
    # regex pre-check - see its own docstring for why: a chapter-list/
    # table-of-contents page (no real sentences, just headings run
    # together) produced genuinely bad audio no TTS-side fix could rescue,
    # and the regex heuristic that used to decide "does this page need
    # rewriting" got confirmed-live-wrong twice in one session on two
    # different books, for two different reasons. The model itself now
    # makes that call and either returns real prose unchanged or rewrites a
    # non-prose page into a short, honestly-grounded spoken description -
    # see that function's own "never invent" constraint.
    rewrite_cfg = await config_sdk.get_stage_config(
        AGENT_ID, 'rewrite_page', {'model': 'qwen3:8b-16k', 'temperature': 0.3, 'base_url': OLLAMA_URL},
        description='Reasoning model that decides whether a page is real prose (left unchanged) or a table-of-contents/heading page (rewritten into a short, honest spoken description) before narration.',
    )
    speech_text = await tts.rewrite_for_speech(page_text, rewrite_cfg)

    # Emotion tagging (<laugh> <chuckle> <sigh> <gasp> <yawn> <cough>
    # <sniffle> <groan> - Orpheus's own literal text tokens): a second,
    # small model inserts tags inline before Orpheus ever sees the text -
    # Orpheus itself only turns text into audio, it never decides where an
    # emotion belongs. Passed into tts.synthesize() below, not applied
    # here on the whole page - see tts.synthesize()'s own docstring for
    # why this has to run windowed, not per-page or per-chunk. Every
    # tagged sentence is verified word-for-word before being kept
    # (tts._accept_tagged_piece) regardless of which model runs here, so
    # switching models can't silently reintroduce the word-deletion
    # corruption qwen3:8b-16k showed earlier in testing - a bad tag now
    # gets discarded in code, not trusted on the model's word alone.
    # Fails open to untagged text on any error, same as rewrite_for_speech
    # above - a quality enhancement, never a reason a page fails to read.
    emotion_cfg = await config_sdk.get_stage_config(
        AGENT_ID, 'add_emotion_tags', {'model': 'gemma4:e2b', 'temperature': 0.2, 'base_url': OLLAMA_URL},
        description='Small model that inserts Orpheus emotion tags into each TTS chunk - restricted to real character reactions during dialogue, never scenery/idiom.',
    )

    tts_cfg = await config_sdk.get_stage_config(
        AGENT_ID, 'synthesize_speech', {
            'model': 'legraphista/Orpheus:3b-ft-q8', 'base_url': OLLAMA_URL,
            # temperature/top_p: Canopy Labs' own documented defaults for
            # Orpheus. repetition_penalty: raised from their documented 1.1
            # to 1.3 after live testing this session - 1.1 still produced
            # real exact-phrase repetition loops (see tts.py's own
            # _generate_tokens docstring), 1.3 measurably fixed them on the
            # same real sentences.
            'temperature': 0.6, 'top_p': 0.9, 'repetition_penalty': 1.3,
            # 'engine'/'voicebox_profile_id': the Chatterbox Turbo
            # alternative path - see tts.py's own VOICEBOX_URL comment.
            # 'orpheus' (unset voicebox_profile_id) is the safe default for
            # a fresh install; an existing deployment's own already-seeded
            # stage config isn't touched by changing this default - switch
            # it live via config_sdk/the dashboard instead.
            'engine': 'orpheus', 'voicebox_profile_id': None,
        },
        description='Which Ollama-served TTS model reads each page aloud, and Orpheus\'s own generation knobs (temperature/top_p/repetition_penalty) that control how varied vs. reliable the delivery is.',
    )
    # job['voice'] is '' unless this job explicitly chose a voice at
    # start_reading time (see that skill's own docstring) - resolved live
    # from config_sdk here, not frozen at job creation, so changing
    # default_voice in the dashboard actually takes effect on the very
    # next page for every job that never explicitly overrode it. Confirmed
    # live this session: the old behavior (store the resolved default
    # forever) meant a dashboard voice change had zero effect on any
    # already-started job - the value lives in Mongo specifically so it
    # stays live, not so it gets copied into SQLite once and forgotten.
    voice = job['voice'] or await config_sdk.get_constant(
        AGENT_ID, 'default_voice', tts.DEFAULT_VOICE,
        description='Which voice a reading job uses when none is specified or the requested one isn\'t in available_voices.',
    )
    if clearer:
        # A one-off request ("reread that more clearly"), never persisted -
        # unlike speech_rate below, this only affects THIS single call.
        # Lower temperature/repetition_penalty trade some of Orpheus's own
        # expressiveness for a more careful, less variable reading - the
        # same knobs tts_cfg already exposes, just nudged down for one page.
        tts_cfg = {**tts_cfg, 'temperature': min(tts_cfg.get('temperature', 0.6), 0.4), 'repetition_penalty': max(tts_cfg.get('repetition_penalty', 1.3), 1.4)}
    # speech_rate is a persistent per-job preference (reread_page.py's own
    # "slower" request sets it via db.set_reading_job_speech_rate) - every
    # page for this job, nightly or on-demand, keeps that pace until changed
    # back, unlike `clearer` above.
    audio = await tts.synthesize(speech_text, voice, tts_cfg, emotion_cfg=emotion_cfg, rate=job.get('speech_rate', 1.0))
    # WhatsApp's voice-note (PTT) bubble has no caption field the way
    # send_document's does - confirmed live, OpenWA's own send-audio API
    # takes no caption param at all (mesh/lib/utilities/whatsapp/
    # openwa_service.py's send_voice()). A short text message announcing
    # the page, sent right before the audio, is the only way to tell the
    # listener what they're about to hear before they hear it.
    title = job['source_filename'].split('/', 1)[-1].rsplit('.', 1)[0].replace('_', ' ')
    await _agent.send_message_to(chat_id, f'📖 {title} — page {page_number}')
    await _agent.send_voice_to(chat_id, audio)
    # Fire-and-forget, after delivery - see eval_quality.py's own module
    # docstring. Checked against speech_text (what was actually handed to
    # TTS - the post-rewrite, e.g. a table-of-contents page's short spoken
    # description), not page_text (the book's own raw content): this eval
    # answers "did the audio say what we told it to say," a separate
    # question from "was the rewrite itself faithful to the page," which
    # rewrite_for_speech()'s own strict-grounding prompt already covers.
    eval_quality.schedule(speech_text, audio, job['id'], job['source_filename'], page_number)
    return page_text


async def _register_next_reading(reading_job_id: str) -> str:
    """Just the nightly re-registration - no quiz. Its own function so
    read_range.py's burst reads can keep the ongoing nightly habit going
    from wherever the bookmark ends up, without also going through
    _schedule_quiz_and_next_reading()'s per-page quiz generation (a 10-page
    binge scheduling 10 same-minute quiz messages for tomorrow morning is
    not what anyone asking to catch up on a book wants)."""
    token = permissions.mint_token(AGENT_ID, 'adiyan_reader_service')
    cron_trigger_url = await config_sdk.get_constant(
        AGENT_ID, 'cron_trigger_url', CRON_TRIGGER_URL,
        description='URL of the cron_trigger MCP server that fires this agent\'s nightly reading and next-day quiz.',
    )
    reading_hour = await config_sdk.get_constant(AGENT_ID, 'reading_hour', 0)
    next_reading_at = croniter(f'0 {int(reading_hour)} * * *', datetime.now(timezone.utc)).get_next(datetime).isoformat()
    await call_tool(cron_trigger_url, 'register_trigger', {
        'job_id': reading_job_id,
        'invoke_at': next_reading_at,
        'target_agent_url': AGENT_URL,
        'skill_id': 'read_next_page',
        'params': {'reading_job_id': reading_job_id},
    }, token=token)
    return next_reading_at


async def _schedule_quiz_and_next_reading(reading_job_id: str, job: Dict[str, Any], page_text: str, page_number: int) -> Dict[str, Any]:
    """Question generation + both cron_trigger registrations - the policy
    half of a normal single-page read, split out from
    _synthesize_and_send_page() (the narration itself) so run() can call
    both in sequence for a nightly/on-demand page, while read_range.py's
    burst reads skip the quiz half entirely (see _register_next_reading's
    own docstring)."""
    conn = db.connect(state_db_path(AGENT_ID))
    db.advance_page(conn, reading_job_id, page_number)

    questions_preloaded = 0
    # stop_reading.py's questions_only=True path sets this False on the job
    # while leaving `active` alone - narration keeps going below regardless,
    # only the question half is skipped, no dashboard/questions_enabled=True
    # default is used verbatim for older jobs that predate this field
    # (dict.get, never a KeyError on a pre-existing document).
    if job.get('questions_enabled', True):
        question_cfg = await config_sdk.load_stage_configs(
            AGENT_ID, {'generate_questions': {'model': 'qwen3:8b-16k', 'temperature': 0.4}},
        )
        question_count = await config_sdk.get_constant(
            AGENT_ID, 'questions_per_page', 3,
            description='How many comprehension questions get generated and sent the morning after each page.',
        )
        questions = await _generate_questions(page_text, question_cfg['generate_questions'], int(question_count))

        quiz_hour = await config_sdk.get_constant(
            AGENT_ID, 'quiz_hour', 9,
            description='Hour of day (0-23, local server time) the next-day comprehension questions are dispatched.',
        )
        dispatch_at = croniter(f'0 {int(quiz_hour)} * * *', datetime.now(timezone.utc) + timedelta(minutes=1)).get_next(datetime).isoformat()
        db.add_questions(conn, reading_job_id, page_number, questions, dispatch_at)

        token = permissions.mint_token(AGENT_ID, 'adiyan_reader_service')
        cron_trigger_url = await config_sdk.get_constant(
            AGENT_ID, 'cron_trigger_url', CRON_TRIGGER_URL,
            description='URL of the cron_trigger MCP server that fires this agent\'s nightly reading and next-day quiz.',
        )
        await call_tool(cron_trigger_url, 'register_trigger', {
            'job_id': f'{reading_job_id}-quiz-{page_number}',
            'invoke_at': dispatch_at,
            'target_agent_url': AGENT_URL,
            'skill_id': 'dispatch_questions',
            'params': {'reading_job_id': reading_job_id, 'page_number': page_number},
        }, token=token)
        questions_preloaded = len(questions)

    next_reading_at = await _register_next_reading(reading_job_id)
    return {'questions_preloaded': questions_preloaded, 'next_reading_at': next_reading_at}


# How many consecutive missing pages get probed past before a book is
# actually treated as finished - confirmed live as a real, isolated
# ingestion gap: Don Quixote's own page 3 came back missing from Qdrant
# while pages 1, 2, 4, 5... all existed with real content. get_book_page's
# found=False used to be trusted, on its own, as proof the book had ended -
# silently deactivating a 349-page reading job after page 2. A small
# forward probe distinguishes "one page didn't make it into the database"
# from "there is genuinely nothing more" (nothing in the whole window
# exists either) without needing to know the book's real total page count
# up front.
_MAX_PAGE_GAP = 5


async def _find_readable_page(
    job: Dict[str, Any], chat_id: str, start_page: int, max_gap: int = _MAX_PAGE_GAP,
) -> tuple:
    """(page_number, page_text) for the first page at or after start_page
    that actually exists and was sent, or (None, None) if nothing in the
    whole [start_page, start_page + max_gap) window exists either - a
    genuine end of book, not just a gap. Reuses
    _synthesize_and_send_page() itself as the existence check (it returns
    None immediately, before any synthesis or send, on a real miss - see
    its own docstring) so a probe that finds nothing never has a side
    effect, and the one that does find a page has already fetched,
    synthesized, and sent it in the same call - no separate existence
    check needed."""
    for candidate in range(start_page, start_page + max_gap):
        text = await _synthesize_and_send_page(job, chat_id, candidate)
        if text is not None:
            if candidate != start_page:
                logger.warning(
                    f"Page {start_page} missing for {job['source_filename']!r} - skipped ahead to "
                    f"page {candidate} instead of treating this as the end of the book."
                )
            return candidate, text
    return None, None


async def run(reading_job_id: str) -> Dict[str, Any]:
    conn = db.connect(state_db_path(AGENT_ID))
    job = db.get_reading_job(conn, reading_job_id)
    if job is None or not job['active']:
        return {'reading_job_id': reading_job_id, 'status': 'inactive', 'result_summary': 'Reading job not found or already stopped.'}

    # See db.try_acquire_reading_lock's own docstring for the real incident
    # this guards against: a rapid-fire repeat of "read the next page"
    # arriving before the first request's TTS synthesis (and its eventual
    # db.advance_page) has finished reading the SAME stale current_page and
    # sending the SAME page twice. A lock miss here is reported back as
    # 'busy', not silently dropped or retried - handle_message.py's own
    # read_now/read_range replies surface it as "still working on it."
    if not db.try_acquire_reading_lock(conn, reading_job_id):
        return {'reading_job_id': reading_job_id, 'status': 'busy', 'result_summary': "Still working on your last page - give it a moment."}

    try:
        next_page = job['current_page'] + 1
        chat_id = await _agent.resolve_chat_id(job['phone_number'])
        if chat_id is None:
            return {'reading_job_id': reading_job_id, 'status': 'failed', 'result_summary': f"Could not resolve WhatsApp chat for {job['phone_number']}."}

        actual_page, page_text = await _find_readable_page(job, chat_id, next_page)
        if actual_page is None:
            db.deactivate_reading_job(conn, reading_job_id)
            await _agent.send_message_to(chat_id, f"We've finished reading {job['source_filename']} together - every page has been read out. 📖")
            return {'reading_job_id': reading_job_id, 'status': 'completed', 'result_summary': 'Book finished, reading job deactivated.'}

        schedule_result = await _schedule_quiz_and_next_reading(reading_job_id, job, page_text, actual_page)

        return {
            'reading_job_id': reading_job_id,
            'status': 'completed',
            'page_sent': actual_page,
            'questions_preloaded': schedule_result['questions_preloaded'],
            'next_reading_at': schedule_result['next_reading_at'],
            'result_summary': f"Sent page {actual_page} of {job['source_filename']} as a voice note.",
        }
    finally:
        db.release_reading_lock(conn, reading_job_id)
