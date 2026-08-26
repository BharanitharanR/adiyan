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
from typing import Any, Dict, List

from croniter import croniter
from langchain_ollama import ChatOllama
from pydantic import BaseModel, Field

from mesh.adiyan_reader import db, tts
from mesh.adiyan_reader.constants import (
    AGENT_ID, AGENT_URL, CRON_TRIGGER_URL, MEMORY_AGENT_URL, OLLAMA_URL, OPENWA_SESSION_NAME, OPENWA_URL,
)
from mesh.lib import config_sdk, permissions
from mesh.lib.a2a_client import call_agent
from mesh.lib.mcp_client import call_tool
from mesh.lib.paths import state_db_path
from mesh.lib.utilities.whatsapp.openwa_service import OpenWAService


class ComprehensionQuestions(BaseModel):
    questions: List[str] = Field(description="Short comprehension/reflection questions about the page's actual content - nothing outside it.")


async def _generate_questions(page_text: str, cfg: Dict[str, Any], count: int) -> List[str]:
    model = ChatOllama(
        model=cfg['model'], base_url=OLLAMA_URL, temperature=cfg['temperature'],
    ).with_structured_output(ComprehensionQuestions)
    result = await model.ainvoke(
        f'Here is a page from a book someone was just read out loud:\n\n"""{page_text}"""\n\n'
        f'Write exactly {count} short comprehension questions about what this specific page actually '
        'said - nothing about the rest of the book, nothing invented. Someone who listened to this '
        'page should be able to answer each one directly from it.'
    )
    return result.questions[:count]


async def run(reading_job_id: str) -> Dict[str, Any]:
    conn = db.connect(state_db_path(AGENT_ID))
    job = db.get_reading_job(conn, reading_job_id)
    if job is None or not job['active']:
        return {'reading_job_id': reading_job_id, 'status': 'inactive', 'result_summary': 'Reading job not found or already stopped.'}

    next_page = job['current_page'] + 1
    token = permissions.mint_token(AGENT_ID, 'service')
    page_result = await call_agent(MEMORY_AGENT_URL, 'get_book_page', {
        'source_filename': job['source_filename'], 'page_number': next_page,
    }, token=token)

    openwa = OpenWAService(base_url=OPENWA_URL, api_key='', session_name=OPENWA_SESSION_NAME)
    chat_id = await openwa.resolve_chat_id(job['phone_number'])
    if chat_id is None:
        return {'reading_job_id': reading_job_id, 'status': 'failed', 'result_summary': f"Could not resolve WhatsApp chat for {job['phone_number']}."}

    if not page_result.get('found'):
        db.deactivate_reading_job(conn, reading_job_id)
        await openwa.send_message(chat_id, f"We've finished reading {job['source_filename']} together - every page has been read out. 📖")
        return {'reading_job_id': reading_job_id, 'status': 'completed', 'result_summary': 'Book finished, reading job deactivated.'}

    page_text = page_result['text']

    # speech_text is what actually gets narrated - page_text (the real page
    # content) stays untouched below for _generate_questions(), which needs
    # to stay grounded in what the page actually says, not a short spoken
    # rewrite of it.
    speech_text = page_text
    if not tts.looks_like_prose(page_text):
        # Confirmed live this session: a chapter-list/table-of-contents page
        # (no real sentences, just headings run together) produced genuinely
        # bad audio even with every TTS-side fix applied - the input itself
        # had nothing readable in it. Rewritten into a short, honestly-
        # grounded spoken description instead of narrating raw fragments -
        # see tts.rewrite_for_speech()'s own docstring for the "never
        # invent" constraint this stays under. Only paid for non-prose
        # pages; a normal prose page never reaches this branch at all.
        rewrite_cfg = await config_sdk.get_stage_config(
            AGENT_ID, 'rewrite_page', {'model': 'qwen3:8b-16k', 'temperature': 0.3, 'base_url': OLLAMA_URL},
            description='Reasoning model that turns a non-prose page (table of contents, chapter-heading list) into a short, honest spoken description instead of reading raw fragments aloud.',
        )
        speech_text = await tts.rewrite_for_speech(page_text, rewrite_cfg)

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
        },
        description='Which Ollama-served TTS model reads each page aloud, and Orpheus\'s own generation knobs (temperature/top_p/repetition_penalty) that control how varied vs. reliable the delivery is.',
    )
    audio = await tts.synthesize(speech_text, job['voice'], tts_cfg)
    await openwa.send_voice(chat_id, audio)
    db.advance_page(conn, reading_job_id, next_page)

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
    db.add_questions(conn, reading_job_id, next_page, questions, dispatch_at)

    cron_trigger_url = await config_sdk.get_constant(
        AGENT_ID, 'cron_trigger_url', CRON_TRIGGER_URL,
        description='URL of the cron_trigger MCP server that fires this agent\'s nightly reading and next-day quiz.',
    )
    await call_tool(cron_trigger_url, 'register_trigger', {
        'job_id': f'{reading_job_id}-quiz-{next_page}',
        'invoke_at': dispatch_at,
        'target_agent_url': AGENT_URL,
        'skill_id': 'dispatch_questions',
        'params': {'reading_job_id': reading_job_id, 'page_number': next_page},
    }, token=token)

    reading_hour = await config_sdk.get_constant(AGENT_ID, 'reading_hour', 0)
    next_reading_at = croniter(f'0 {int(reading_hour)} * * *', datetime.now(timezone.utc)).get_next(datetime).isoformat()
    await call_tool(cron_trigger_url, 'register_trigger', {
        'job_id': reading_job_id,
        'invoke_at': next_reading_at,
        'target_agent_url': AGENT_URL,
        'skill_id': 'read_next_page',
        'params': {'reading_job_id': reading_job_id},
    }, token=token)

    return {
        'reading_job_id': reading_job_id,
        'status': 'completed',
        'page_sent': next_page,
        'questions_preloaded': len(questions),
        'next_reading_at': next_reading_at,
        'result_summary': f"Sent page {next_page} of {job['source_filename']} as a voice note.",
    }
