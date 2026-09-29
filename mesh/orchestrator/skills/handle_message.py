"""
handle_message's real body. Three stages: image/document intent (if an
image is attached, decide what it means - see mesh/lib/vision.py's
classify_image; a document's intent is never ambiguous the same way, so it
skips straight to Knowledge Bank - this is a routing decision, so it
belongs here, not in WhatsApp MCP, which only knows WhatsApp send/receive
mechanics), the WhatsApp rules engine (rules_engine.py - registration/
unregistration and the owner bypass), and - only for a message that passes
the gate - routing to the right agent (router.py), forwarding the raw text
(letting that agent's own classify_skill/extract_parameters interpret it -
no duplicated NLU here), humanizing the structured result, and replying via
the whatsapp MCP server's send_message (or send_document) tool.

The one real difference from mesh/whatsapp_connector/'s retired version:
delivery goes through an MCP tool call, not a direct OpenWAService import -
this component knows nothing about WhatsApp's own API, only that something
called 'whatsapp' exposes send_message/send_document tools. That's the
actual point of "wired to WhatsApp as an MCP."

Delivery convention: any target agent's skill result that carries a
content_b64 key is understood as "deliver this as a file," not text -
mesh/memory/skills/share_document.py is the first skill to use this, but
nothing here is specific to that one skill_id; any future skill from any
agent that wants to hand back a file just needs to return
{content_b64, filename, mimetype} the same way.
"""
import base64
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from a2a.types import AgentSkill
from indic_transliteration import detect as script_detect
from indic_transliteration import sanscript
from pydantic import BaseModel, Field
from rapidfuzz import fuzz

from mesh.lib import chat_cache, config_sdk, customer_record, permissions, persona_hook, vision
from mesh.lib.a2a_client import call_agent, call_agent_with_text
from mesh.lib.agent_sdk import AdiyanAgent
from mesh.lib.audio_transcribe import language_display_name, transcribe_audio
from mesh.lib.config import load_runtime_config
from mesh.lib.errors import describe_exception
from mesh.lib.mcp_client import call_tool
from mesh.lib.paths import state_db_path
from mesh.lib.skill_router import classify, extract
from mesh.orchestrator import db, router, rules_engine
from mesh.orchestrator.constants import AGENT_ID, WHATSAPP_MCP_URL
from mesh.orchestrator.humanize import humanize
from mesh.orchestrator.router import route_to_agent

AGENT_CODE_DIR = Path(__file__).parent.parent
logger = logging.getLogger('HandleMessage')
_agent = AdiyanAgent(AGENT_ID)

# The logo sent alongside a fresh registration's welcome reply - read once
# at import time (a static asset baked into the repo, not something that
# changes at runtime the way config_sdk-backed values do) and cached as
# base64, ready to hand straight to send_document's own content_b64 field.
_LOGO_PATH = Path(__file__).parent.parent.parent / 'config_server' / 'static' / 'adiyan_logo.png'
try:
    _LOGO_B64 = base64.b64encode(_LOGO_PATH.read_bytes()).decode('ascii')
except Exception as e:
    logger.warning(f'Could not load Adiyan logo for the registration reply, sending text-only: {e}')
    _LOGO_B64 = None

# A single-skill pool fed to skill_router.classify() purely as a yes/no
# check: does this upload's caption read as an actual instruction (analyze,
# review, find X), or is it just a plain label/description ("this is
# Bharani's aadhar")? Reuses the same classify machinery every other "which
# of these does this text mean" decision in this codebase already uses,
# rather than a bespoke classifier.
_UPLOAD_INSTRUCTION_SKILLS = [
    AgentSkill(
        id='analyze_upload',
        name='Analyze Upload',
        description=(
            'The caption is an actual instruction to analyze, review, critique, '
            'summarize, or find something in the document being uploaded - not '
            'just a plain label or description of what it is.'
        ),
        tags=['upload', 'instruction'],
        examples=[
            'Analyse this presentation and share the mistakes you find',
            'Find the spelling mistakes in this',
            'Review this contract for issues',
            'Summarize this document',
        ],
        input_modes=['text/plain'],
        output_modes=['application/json'],
    ),
]

# Single-skill classify pool, same shape as _UPLOAD_INSTRUCTION_SKILLS -
# checked BEFORE the normal knowledge-base ingestion path, and only for the
# owner (see run()'s own call site: never even classified for a customer
# upload, since applying a vertical spec is an owner-only action end to
# end - config_agent's own permission check would reject it anyway, this
# just avoids spending a classify() call on every customer upload to learn
# that). Matching "analyze_upload" instead of this would misfire on a
# real business-vertical YAML - handled here, deliberately checked first,
# rather than trying to make one classify pool cover both intents at once.
_VERTICAL_SPEC_SKILLS = [
    AgentSkill(
        id='apply_vertical_spec',
        name='Apply Vertical Spec',
        description=(
            'The caption says this uploaded file is a business/vertical configuration '
            'spec to apply to Adiyan itself - not an ordinary document to search or read. '
            'Only matches an explicit instruction to configure, activate, or apply a '
            'business persona/vertical, never a plain document upload that merely mentions '
            'a business in its content.'
        ),
        tags=['config', 'vertical', 'persona'],
        examples=[
            'Apply this business spec',
            'This is my vertical config, activate it',
            'Set up Adiyan with this business persona',
            'Load this as my customer-facing configuration',
        ],
        input_modes=['text/plain'],
        output_modes=['application/json'],
    ),
]

# Same shape as _UPLOAD_INSTRUCTION_SKILLS: a single-skill pool fed to
# skill_router.classify() as a yes/no check, this time for "does this plain
# text message mean start reading me a book." Deliberately NOT added to
# mesh/adiyan_reader/skills_catalog.py's own (empty) get_skills() - that
# emptiness is intentional (see that file's own docstring: start_reading
# needs an already-resolved source_filename, never a raw guess from an
# extraction schema). This classify step only decides intent; the book
# reference it extracts still has to pass through Memory Agent's
# resolve_document before it's trusted as a real key - see
# _resolve_book_reading_request() and _start_book_reading() below.
_BOOK_READING_SKILLS = [
    AgentSkill(
        id='start_book_reading',
        name='Start Book Reading',
        description=(
            'The caller wants Adiyan to start reading a book to them - a nightly '
            'voice note of the next page, read out loud. Only matches an actual '
            'request to begin this, not a question about how it works or a '
            'passing mention of a book title.'
        ),
        tags=['adiyan_reader', 'book'],
        examples=[
            'Read me the power of now every night',
            'Can you read this book to me',
            'Start reading me the book I uploaded',
            'Read me a book, next page',
        ],
        input_modes=['text/plain'],
        output_modes=['application/json'],
    ),
]


# Real thresholds observed this session, transliterating every real
# mis-transcribed spelling caught so far and scoring it against 'adiyan'
# with rapidfuzz.fuzz.ratio(): 'अडियान'->92.3, 'அடியான்'/'அடியன்'->85.7,
# 'அரியான்' (the hardest real case, "Ariyan", a genuine mishearing) and
# 'आदியான' ->76.9. 65 catches every one of those.
#
# Score alone isn't enough though - confirmed live during testing that a
# short, common Tamil address word ('ஐயா', "sir"/"hey") romanizes to
# 'aiya' and scores 80.0, comfortably inside that same range, a real false
# positive. Every genuine variant's romanized form was 6-8 characters
# (fuzz.ratio's own edit-distance-based scoring makes short strings score
# high on very few character differences); a battery of 13 common Tamil/
# Hindi words plausibly appearing in a voice note ('ஐயா', 'அண்ணா', 'யார்',
# 'सर', 'क्या', etc.) all romanized to 3-8 characters but scored 20-60
# EXCEPT 'ஐயா' itself - the length floor is what actually separates it
# from every true positive, not the fuzzy score.
_SUMMON_FUZZY_THRESHOLD = 65.0
_SUMMON_MIN_ROMANIZED_LEN = 6
_SUMMON_STRIP_CHARS = '.,!?;:।'


def _word_matches_spoken_summon(
    word: str, threshold: float = _SUMMON_FUZZY_THRESHOLD, min_len: int = _SUMMON_MIN_ROMANIZED_LEN,
) -> bool:
    """True if `word` (one whitespace-split token from a Whisper
    transcription) is plausibly a mangled rendering of "Adiyan" in ANY
    script Whisper might transcribe it into - Devanagari, Tamil, Latin, or
    anything else indic_transliteration's own detect() recognizes.

    Exact-string matching against a growing list (this function's own
    predecessor) proved structurally wrong for an out-of-vocabulary proper
    noun: three fresh real voice notes produced three brand-new spellings
    never seen before ('अडियान', 'அரியான்' "Ariyan", 'அடியான்'), none
    matching the existing list. "Adiyan" isn't a real word in Tamil or
    Hindi, so ASR has no dictionary entry to anchor its output to - it
    approximates the sounds it hears, differently each time. Transliterate-
    then-fuzzy-match targets the actual invariant (what the word SOUNDS
    like) instead of chasing an unbounded set of exact spellings.

    min_len (see this module's own threshold comment above) rejects short
    words purely on length before the fuzzy score even matters - not a
    tunable to loosen casually, it's the one thing separating a real
    summon attempt from an ordinary short word that happens to sound
    similar.

    detect.detect() on a genuinely random unrelated word can raise or
    return a scheme this doesn't handle well - fails open to "no match"
    (never crashes the caller), same as every other classify-style
    resolver in this module."""
    word = word.strip(_SUMMON_STRIP_CHARS)
    if not word:
        return False
    try:
        scheme = script_detect.detect(word)
        romanized = sanscript.transliterate(word, scheme, sanscript.HK).lower()
    except Exception:
        romanized = word.lower()
    if len(romanized) < min_len:
        return False
    return fuzz.ratio('adiyan', romanized) >= threshold


# Own classify pool, checked BEFORE _BOOK_READING_SKILLS in the elif chain
# below - "stop reading Crime and Punishment" must never be classified
# against start_book_reading's own description just because both mention a
# book and the word "reading".
_STOP_READING_SKILLS = [
    AgentSkill(
        id='stop_book_reading',
        name='Stop Book Reading',
        description=(
            'The caller wants to stop AdiyanReader\'s nightly voice-note readings - for a '
            'specific book if they name one, or their one active book if they only have '
            'one. May instead ask to stop only the next-morning comprehension questions '
            'while the nightly reading itself keeps going. Only matches an explicit '
            'stop/cancel/unsubscribe request, never a question about how the schedule '
            'works or a request to start/pause mid-page.'
        ),
        tags=['adiyan_reader', 'book'],
        examples=[
            'Stop reading me the power of now',
            'Stop sending me audio',
            'Please stop the book',
            'Unsubscribe me from the nightly reading',
            'Stop asking me questions but keep reading the book',
            'No more comprehension questions for Crime and Punishment',
        ],
        input_modes=['text/plain'],
        output_modes=['application/json'],
    ),
]


class _StopReadingRequest(BaseModel):
    book_reference: Optional[str] = Field(
        default=None,
        description=(
            "How the caller referred to the book to stop, if they named one - a title, "
            "partial title, or description. Leave unset if they didn't name a specific "
            "book (e.g. a bare 'stop reading me' with only one book active)."
        ),
    )
    questions_only: bool = Field(
        default=False,
        description=(
            "True only if the caller explicitly asked to stop just the comprehension "
            "questions/quiz while the nightly reading itself keeps going - e.g. 'stop "
            "asking me questions but keep reading'. False for a plain 'stop reading'/"
            "'stop the book'/'stop sending audio', which stops both."
        ),
    )
    all_books: bool = Field(
        default=False,
        description=(
            "True if the caller said 'all'/'both'/'all of them'/'everything' - answering a "
            "previous 'more than one active book - which one?' question by asking for every "
            "book, not one specific one. Only ever true when the message is actually replying "
            "to that kind of question; false otherwise."
        ),
    )


# Checked BEFORE _BOOK_READING_SKILLS in the elif chain below, same
# reasoning _STOP_READING_SKILLS' own comment already documents for itself -
# "start all over again" must never be classified against start_book_reading
# and taken as a literal title. Confirmed live as a real bug, not a
# hypothetical one: a genuine subscriber said exactly "start all over again"
# and got "I couldn't find 'start all over again' among your uploaded books,
# or on Project Gutenberg" back - there was no restart intent to classify
# against at all before this skill existed.
_RESTART_READING_SKILLS = [
    AgentSkill(
        id='restart_book_reading',
        name='Restart Book Reading',
        description=(
            'The caller wants to restart the book they\'re currently being read, from page 1 - '
            'for a specific book if they name one, or their one active book if they only have '
            'one. Only matches an explicit restart/start-over request for an ALREADY-ACTIVE '
            'book, never a request to start reading a brand-new book for the first time (that\'s '
            'start_book_reading) or a question about how the schedule works.'
        ),
        tags=['adiyan_reader', 'book'],
        examples=[
            'Start all over again',
            'Start over',
            'Restart the book',
            'Can we start from the beginning again',
            'Begin the book again from page one',
            'Restart Crime and Punishment from the start',
        ],
        input_modes=['text/plain'],
        output_modes=['application/json'],
    ),
]


class _RestartReadingRequest(BaseModel):
    book_reference: Optional[str] = Field(
        default=None,
        description=(
            "How the caller referred to the book to restart, if they named one - a title, "
            "partial title, or description. Leave unset if they didn't name a specific book "
            "(e.g. a bare 'start all over again' with only one book active)."
        ),
    )
    all_books: bool = Field(
        default=False,
        description=(
            "True if the caller said 'all'/'both'/'all of them' - answering a previous "
            "'more than one active book - which one?' question by asking to restart every "
            "book, not one specific one. Only ever true when replying to that kind of question."
        ),
    )


# Checked BEFORE _BOOK_READING_SKILLS too, same reasoning as
# _RESTART_READING_SKILLS' own comment - "read that again" or "slower" must
# never be classified as a request to start reading a book literally titled
# that.
_REREAD_SKILLS = [
    AgentSkill(
        id='reread_page',
        name='Reread Current Page',
        description=(
            'The caller wants to hear the page they were JUST read again - not the next '
            'page, the same one - optionally with more clarity/slower, or both. Only matches '
            'an explicit request to repeat/reread the current page, never a request for the '
            'next page (read_now) or to start/restart a book.'
        ),
        tags=['adiyan_reader', 'book'],
        examples=[
            'Read that again',
            'Can you reread that page',
            'Say that again more clearly',
            'Read it slower',
            'Read that again, slower',
            'I didn\'t catch that, read it again',
        ],
        input_modes=['text/plain'],
        output_modes=['application/json'],
    ),
]


class _RereadPageRequest(BaseModel):
    book_reference: Optional[str] = Field(
        default=None,
        description=(
            "How the caller referred to the book to reread, if they named one. Leave unset "
            "if they didn't name a specific book (e.g. a bare 'read that again' with only "
            "one book active)."
        ),
    )
    clearer: bool = Field(
        default=False,
        description="True if the caller asked for more clarity - 'more clearly', 'I couldn't understand it', 'say that again properly'.",
    )
    slower: bool = Field(
        default=False,
        description="True if the caller asked for a slower pace - 'slower', 'read it slowly', 'can you slow down'. This becomes a lasting preference for every future page, not just this reread.",
    )
    all_books: bool = Field(
        default=False,
        description=(
            "True if the caller said 'all'/'both'/'all of them' - answering a previous "
            "'more than one active book - which one?' question by asking to reread every "
            "book's current page, not one specific one. Only ever true when replying to that "
            "kind of question."
        ),
    )


# Own classify pool, no extraction step needed - who/which book is already
# resolved from the sender's own phone number (adiyan_reader's own
# get_active_reading_jobs_by_phone), never guessed from the text.
_VOICE_SAMPLES_SKILLS = [
    AgentSkill(
        id='request_voice_samples',
        name='Request Voice Samples',
        description=(
            'The caller wants to hear samples of the different narrator voices available, to '
            'pick one - not a request to change their voice to a specific one already named '
            '(that\'s change_voice), and not a question about how many voices exist.'
        ),
        tags=['adiyan_reader', 'book'],
        examples=[
            'Can I hear different voices',
            'What voices do you have',
            'Let me choose a voice',
            'Play me some voice samples',
            'I want to pick a different narrator',
        ],
        input_modes=['text/plain'],
        output_modes=['application/json'],
    ),
]


async def _resolve_voice_samples_request(text: str, cfg: Dict[str, Any]) -> bool:
    """True only for an explicit request to hear voice samples - same
    degrade-on-failure contract every other classify-based resolver in this
    module follows."""
    try:
        choice = await classify(text, _VOICE_SAMPLES_SKILLS, cfg['book_reading_intent'])
    except Exception as e:
        logger.error(f'Voice-samples intent classification failed: {describe_exception(e)}')
        return False
    return choice.skill_id == 'request_voice_samples'


async def _send_voice_samples(chat_id: str, from_number: Optional[str], tier: str, vertical_id: Optional[str] = None) -> Optional[str]:
    """Reply text, or None only on a genuinely unexpected failure - same
    convention every other action function in this module follows. The real
    samples are sent directly by adiyan_reader's own voice_samples.py as it
    generates each one - this reply is just the closing "now pick one"
    nudge, reusing that skill's own result_summary."""
    reader_url = router.get_agent_url('adiyan_reader')
    if reader_url is None:
        return "The book reader isn't reachable right now - try again in a moment."

    token = permissions.mint_token(chat_id, tier, vertical_id=vertical_id)
    try:
        result = await call_agent(reader_url, 'voice_samples', {
            'phone_number': from_number or chat_id,
        }, token=token)
    except Exception as e:
        logger.error(f'voice_samples failed for {chat_id}: {describe_exception(e)}')
        return None

    return result.get('result_summary') or "Couldn't generate voice samples right now - try again in a moment."


_CHANGE_VOICE_SKILLS = [
    AgentSkill(
        id='change_voice',
        name='Change Narrator Voice',
        description=(
            'The caller wants to switch to a SPECIFIC narrator voice they already named, for a '
            'book if they name one or their one active book if they only have one - e.g. after '
            'hearing voice samples and picking a favourite. Only matches when a specific voice '
            'name is actually given, never a request to hear samples first (that\'s '
            'request_voice_samples).'
        ),
        tags=['adiyan_reader', 'book'],
        examples=[
            'I like Leah',
            'Use Tara\'s voice',
            'Switch to Jess',
            'Change my voice to Zac',
            'I want Mia to read to me',
        ],
        input_modes=['text/plain'],
        output_modes=['application/json'],
    ),
]


class _ChangeVoiceRequest(BaseModel):
    voice: str = Field(description="The narrator voice name the caller picked, exactly as they said it (e.g. 'Leah', 'tara'). Never invent or guess a name they didn't say.")
    book_reference: Optional[str] = Field(
        default=None,
        description=(
            "How the caller referred to the book to change the voice for, if they named one. "
            "Leave unset if they didn't name a specific book (e.g. a bare 'I like Leah' with "
            "only one book active)."
        ),
    )
    all_books: bool = Field(
        default=False,
        description=(
            "True if the caller said 'all'/'both'/'all of them' - answering a previous "
            "'more than one active book - which one?' question by asking to change the voice "
            "on every book, not one specific one. Only ever true when replying to that kind "
            "of question."
        ),
    )


async def _resolve_change_voice_request(text: str, cfg: Dict[str, Any]) -> Optional[_ChangeVoiceRequest]:
    """None if this message isn't actually a "use this voice" request -
    same degrade-on-failure contract every other classify-based resolver in
    this module follows."""
    try:
        choice = await classify(text, _CHANGE_VOICE_SKILLS, cfg['book_reading_intent'])
    except Exception as e:
        logger.error(f'Change-voice intent classification failed: {describe_exception(e)}')
        return None
    if choice.skill_id != 'change_voice':
        return None
    try:
        return await extract(text, 'change_voice', _ChangeVoiceRequest, cfg['extract_parameters'])
    except Exception as e:
        logger.error(f'Change-voice parameter extraction failed: {describe_exception(e)}')
        return None


async def _change_voice(
    request: _ChangeVoiceRequest, chat_id: str, from_number: Optional[str], tier: str, vertical_id: Optional[str] = None,
) -> Optional[str]:
    """Reply text, or None only on a genuinely unexpected failure - same
    convention every other action function in this module follows."""
    reader_url = router.get_agent_url('adiyan_reader')
    if reader_url is None:
        return "The book reader isn't reachable right now - try again in a moment."

    token = permissions.mint_token(chat_id, tier, vertical_id=vertical_id)
    try:
        result = await call_agent(reader_url, 'change_voice', {
            'phone_number': from_number or chat_id,
            'voice': request.voice,
            'book_reference': request.book_reference,
            'all_books': request.all_books,
        }, token=token)
    except Exception as e:
        logger.error(f'change_voice failed for {chat_id}: {describe_exception(e)}')
        return None

    status = result.get('status')
    if status == 'no_active_job':
        return "You don't have any book being read to you right now - nothing to change the voice on."
    if status in ('ambiguous', 'not_found'):
        books = ', '.join(result.get('active_books', []))
        if status == 'not_found':
            return f"\"{request.book_reference}\" isn't among your active books - you've got: {books}. Which one did you mean?"
        return f"You've got more than one book going - {books}. Which one should I change the voice for?"
    if status == 'invalid_voice':
        return result.get('result_summary') or f"\"{request.voice}\" isn't one of the available voices."
    return result.get('result_summary') or f"Switched to {request.voice} voice."


class _BookReadingRequest(BaseModel):
    book_reference: str = Field(description="How the caller referred to the book - a title, a partial title, or a description like 'the book I uploaded yesterday'. Copy their own wording, don't invent or complete a title they didn't say.")


class _ReadRangeRequest(BaseModel):
    start_page: Optional[int] = Field(default=None, description="The first page number the caller named, if any - e.g. 5 for 'read page 5 to 10'. Leave unset for a bare 'read all'/'read the rest', or for a relative count like 'the next 5 pages' (that's page_count below, not this).")
    end_page: Optional[int] = Field(default=None, description="The last page number the caller named, if any - e.g. 10 for 'read page 5 to 10'. Leave unset for 'read all', an open-ended 'read from page 5 onward', or a relative count (page_count below).")
    page_count: Optional[int] = Field(default=None, description="How many pages the caller asked for, counting from wherever they currently are (or from start_page, if that was also given) - e.g. 5 for 'read me the next 5 pages' or 'send 5 more pages'. Leave unset if they gave explicit page numbers instead (start_page/end_page), or asked for 'all'/'the rest'.")


class _UploadOcrPreference(BaseModel):
    skip_image_scanning: bool = Field(
        default=False,
        description=(
            "True only if the caller's caption explicitly says this upload is "
            "plain digital text and doesn't need image/OCR scanning - e.g. "
            "asking for it to be processed fast, or saying no image scan is "
            "needed, or that it's just text. False by default, and False if "
            "the caption says nothing about this at all - never assume a "
            "document is scan-free just because nothing was said."
        ),
    )


class _UploadVisibilityPreference(BaseModel):
    global_visibility: bool = Field(
        default=False,
        description=(
            "True only if the caller's caption explicitly says this document should be "
            "available to every customer / globally / for everyone - e.g. 'make this "
            "available for every customer', 'this is for all clients', 'set it to global'. "
            "False by default, and False if the caption says nothing about this at all - "
            "never assume a document should be shared with every customer just because "
            "nothing was said."
        ),
    )


class _CustomerObservation(BaseModel):
    note: Optional[str] = Field(
        default=None,
        description=(
            "A short factual sentence, ABOUT the customer, worth remembering later - a stated "
            "preference, an interest, a detail about what they're asking for or considering. Written "
            "in the third person as a plain fact for a future reader (e.g. 'Prefers less spicy food' "
            "or 'Asked about the weekend biryani special'), never as advice, a suggestion, or a "
            "comment on how Adiyan should have replied. Copy only what they actually said; never "
            "invent a detail, a decision, or a commitment they didn't make. None if this exchange has "
            "nothing new worth keeping (most ordinary back-and-forth doesn't)."
        ),
    )


async def _observe_customer(
    text: str, reply: str, vertical_id: str, identity_key: str, extract_cfg: Dict[str, Any],
) -> None:
    """Best-effort, additive customer_section update for a non-owner
    message under an active vertical - mesh/lib/customer_record.py's own
    docstring on why this is the ONLY LLM-inferred write path (a plain
    'notes' list, not an open-ended field set) and why a consequential fact
    never goes through here. Never raises - a failed observation must not
    affect a reply that's already been sent, same convention the
    remember_interaction call just above already follows."""
    try:
        prompt = (
            'A customer just exchanged one message with a business assistant. Extract a fact ABOUT '
            'THE CUSTOMER worth keeping for next time - not a summary of the exchange, not advice on '
            'how to reply.\n\n'
            f'Customer said: "{text}"\n\n'
            f'Assistant replied: "{reply}"'
        )
        observation = await _agent.ask(
            prompt, stage='observe_customer', schema=_CustomerObservation,
            model=extract_cfg['model'], temperature=extract_cfg['temperature'],
        )
        if observation.note:
            await customer_record.append_customer_note(vertical_id, identity_key, observation.note)
    except Exception as e:
        logger.warning(f'Customer observation failed for {identity_key!r} in vertical {vertical_id!r}: {e}')


async def _resolve_stop_reading_request(text: str, cfg: Dict[str, Any]) -> Optional[_StopReadingRequest]:
    """None if this message isn't actually a "stop reading" request - same
    degrade-on-failure contract every other classify-based resolver in this
    module follows (falls through to normal routing, never an error surfaced
    to the sender)."""
    try:
        choice = await classify(text, _STOP_READING_SKILLS, cfg['book_reading_intent'])
    except Exception as e:
        logger.error(f'Stop-reading intent classification failed: {describe_exception(e)}')
        return None
    if choice.skill_id != 'stop_book_reading':
        return None
    try:
        return await extract(text, 'stop_book_reading', _StopReadingRequest, cfg['extract_parameters'])
    except Exception as e:
        logger.error(f'Stop-reading parameter extraction failed: {describe_exception(e)}')
        return None


async def _stop_book_reading(
    request: _StopReadingRequest, chat_id: str, from_number: Optional[str], tier: str, vertical_id: Optional[str] = None,
) -> Optional[str]:
    """Reply text, or None only on a genuinely unexpected failure - same
    convention every other action function in this module follows. Passes
    the caller's own book wording through unresolved (adiyan_reader's
    stop_reading.py matches it against THAT phone's own active jobs itself,
    never a filename guessed here) and lets that skill's own status values
    (no_active_job / ambiguous / not_found / questions_stopped / stopped)
    drive the reply."""
    reader_url = router.get_agent_url('adiyan_reader')
    if reader_url is None:
        return "The book reader isn't reachable right now - try again in a moment."

    token = permissions.mint_token(chat_id, tier, vertical_id=vertical_id)
    try:
        result = await call_agent(reader_url, 'stop_reading', {
            'phone_number': from_number or chat_id,
            'book_reference': request.book_reference,
            'questions_only': request.questions_only,
            'all_books': request.all_books,
        }, token=token)
    except Exception as e:
        logger.error(f'stop_reading failed for {chat_id}: {e}')
        return None

    status = result.get('status')
    if status == 'no_active_job':
        return "You don't have any book being read to you right now - nothing to stop."
    if status in ('ambiguous', 'not_found'):
        books = ', '.join(result.get('active_books', []))
        if status == 'not_found':
            return f"\"{request.book_reference}\" isn't among your active books - you've got: {books}. Which one did you mean?"
        return f"You've got more than one book going - {books}. Which one should I stop?"
    return result.get('result_summary') or f"Stopped {result.get('title', 'that book')}."


async def _resolve_restart_reading_request(text: str, cfg: Dict[str, Any]) -> Optional[_RestartReadingRequest]:
    """None if this message isn't actually a "restart the book" request -
    same degrade-on-failure contract every other classify-based resolver in
    this module follows."""
    try:
        choice = await classify(text, _RESTART_READING_SKILLS, cfg['book_reading_intent'])
    except Exception as e:
        logger.error(f'Restart-reading intent classification failed: {describe_exception(e)}')
        return None
    if choice.skill_id != 'restart_book_reading':
        return None
    try:
        return await extract(text, 'restart_book_reading', _RestartReadingRequest, cfg['extract_parameters'])
    except Exception as e:
        logger.error(f'Restart-reading parameter extraction failed: {describe_exception(e)}')
        return None


async def _restart_book_reading(
    request: _RestartReadingRequest, chat_id: str, from_number: Optional[str], tier: str, vertical_id: Optional[str] = None,
) -> Optional[str]:
    """Reply text, or None only on a genuinely unexpected failure - same
    convention every other action function in this module follows. Passes
    the caller's own book wording through unresolved (adiyan_reader's
    restart_reading.py matches it against THAT phone's own active jobs
    itself, never a filename guessed here) and lets that skill's own status
    values (no_active_job / ambiguous / not_found / restarted) drive the
    reply."""
    reader_url = router.get_agent_url('adiyan_reader')
    if reader_url is None:
        return "The book reader isn't reachable right now - try again in a moment."

    token = permissions.mint_token(chat_id, tier, vertical_id=vertical_id)
    try:
        result = await call_agent(reader_url, 'restart_reading', {
            'phone_number': from_number or chat_id,
            'book_reference': request.book_reference,
            'all_books': request.all_books,
        }, token=token)
    except Exception as e:
        logger.error(f'restart_reading failed for {chat_id}: {e}')
        return None

    status = result.get('status')
    if status == 'no_active_job':
        return "You don't have any book being read to you right now - nothing to restart."
    if status in ('ambiguous', 'not_found'):
        books = ', '.join(result.get('active_books', []))
        if status == 'not_found':
            return f"\"{request.book_reference}\" isn't among your active books - you've got: {books}. Which one did you mean?"
        return f"You've got more than one book going - {books}. Which one should I restart?"
    return result.get('result_summary') or f"Restarted {result.get('title', 'that book')} from page 1."


async def _resolve_reread_page_request(text: str, cfg: Dict[str, Any]) -> Optional[_RereadPageRequest]:
    """None if this message isn't actually a "reread the current page"
    request - same degrade-on-failure contract every other classify-based
    resolver in this module follows."""
    try:
        choice = await classify(text, _REREAD_SKILLS, cfg['book_reading_intent'])
    except Exception as e:
        logger.error(f'Reread-page intent classification failed: {describe_exception(e)}')
        return None
    if choice.skill_id != 'reread_page':
        return None
    try:
        return await extract(text, 'reread_page', _RereadPageRequest, cfg['extract_parameters'])
    except Exception as e:
        logger.error(f'Reread-page parameter extraction failed: {describe_exception(e)}')
        return None


async def _reread_page(
    request: _RereadPageRequest, chat_id: str, from_number: Optional[str], tier: str, vertical_id: Optional[str] = None,
) -> Optional[str]:
    """Reply text, or None only on a genuinely unexpected failure - same
    convention every other action function in this module follows."""
    reader_url = router.get_agent_url('adiyan_reader')
    if reader_url is None:
        return "The book reader isn't reachable right now - try again in a moment."

    token = permissions.mint_token(chat_id, tier, vertical_id=vertical_id)
    try:
        result = await call_agent(reader_url, 'reread_page', {
            'phone_number': from_number or chat_id,
            'book_reference': request.book_reference,
            'clearer': request.clearer,
            'slower': request.slower,
            'all_books': request.all_books,
        }, token=token)
    except Exception as e:
        logger.error(f'reread_page failed for {chat_id}: {describe_exception(e)}')
        return None

    status = result.get('status')
    if status == 'no_active_job':
        return "You don't have any book being read to you right now - nothing to reread."
    if status == 'nothing_read_yet':
        return result.get('result_summary') or "Haven't read any of that book yet - nothing to reread."
    if status in ('ambiguous', 'not_found'):
        books = ', '.join(result.get('active_books', []))
        if status == 'not_found':
            return f"\"{request.book_reference}\" isn't among your active books - you've got: {books}. Which one did you mean?"
        return f"You've got more than one book going - {books}. Which one should I reread?"
    return result.get('result_summary') or "Rereading that page now."


# Fixed, stable suffix each of the four "more than one active book" replies
# ends with - see each action function's own composed 'ambiguous' branch
# above (the book list itself is glued on HERE in orchestrator; the skill's
# own raw result_summary is never what actually gets sent/remembered, only
# this suffix is constant across different customers' different book
# lists). Matching on this - not free-text classification - is what lets a
# bare follow-up like "for all" resolve against the RIGHT pending question
# deterministically: these are fixed Python literals, never LLM-generated,
# so there's nothing to hallucinate here.
_PENDING_BOOK_DISAMBIGUATION: Dict[str, Tuple[str, Any, Any]] = {
    'Which one should I change the voice for?': ('change_voice', _ChangeVoiceRequest, _change_voice),
    'Which one should I stop?': ('stop_book_reading', _StopReadingRequest, _stop_book_reading),
    'Which one should I restart?': ('restart_book_reading', _RestartReadingRequest, _restart_book_reading),
    'Which one should I reread?': ('reread_page', _RereadPageRequest, _reread_page),
}


async def _resolve_pending_book_disambiguation(
    text: str, contact_name: Optional[str], chat_id: str, vertical_id: Optional[str], cfg: Dict[str, Any],
) -> Optional[Tuple[Any, Any]]:
    """(action_fn, request) if the caller's LAST reply FROM US was one of
    the fixed "which book?" disambiguation prompts change_voice/
    stop_reading/restart_reading/reread_page all share, None otherwise
    (chat_cache has nothing for this contact, or their last reply from us
    wasn't one of these prompts).

    Confirmed live as a real gap: "I want leah as my reader" -> (more than
    one active book) "which one should I change the voice for?" -> "for
    all" landed nowhere - that reply has no voice name and no book name in
    it, so every classify() in the elif chain below correctly said "not
    mine" and the customer's actual answer was silently dropped, 8 times
    over one real conversation.

    Reuses chat_cache's own already-populated per-contact history
    (mesh/lib/chat_cache.py), not a second, parallel state store - the
    pending question's own original request text (the turn's own
    user_text, e.g. "I want leah as my reader") is already sitting right
    there, written by this same function's own should_remember branch
    after every reply. chat_cache is platform-level, shared by every agent
    that wants it - the actual gap was never its existence, it was that
    these four resolvers never read it before now.

    Deliberately skips classify() on this path: which skill is pending is
    already known for certain from the prompt's own fixed suffix (see
    _PENDING_BOOK_DISAMBIGUATION's own comment on why that's a
    deterministic match, not a guess) - only extract() runs, over the
    ORIGINAL request text plus the new reply combined, so "for all" or a
    specific book name resolves against real context. Never feeding history
    into classify() itself, which compares against short skill descriptions
    and would only get confused by a history block glued onto it - same
    reasoning the should_remember branch's own history-prepending already
    documents for itself, applied here too."""
    turns = chat_cache.get_recent_turns(contact_name or chat_id, vertical_id)
    if not turns:
        return None
    last_reply = turns[-1]['reply_text'].strip()
    pending = next((v for suffix, v in _PENDING_BOOK_DISAMBIGUATION.items() if last_reply.endswith(suffix)), None)
    if pending is None:
        return None
    skill_id, model_cls, action_fn = pending

    combined_text = f'{turns[-1]["user_text"]}\n{text}'
    try:
        request = await extract(combined_text, skill_id, model_cls, cfg['extract_parameters'])
    except Exception as e:
        logger.error(f'Pending-disambiguation extraction failed for {skill_id!r}: {describe_exception(e)}')
        return None
    return action_fn, request


async def _resolve_book_reading_request(text: str, cfg: Dict[str, Any]) -> Optional[str]:
    """None if this message isn't actually a "start reading" request - the
    caller then falls through to normal routing, same degrade-on-failure
    contract _resolve_upload_instruction() already follows. Otherwise
    returns the caller's own raw book reference exactly as classify/extract
    read it from their words - never a filename, never Memory Agent's own
    key. Resolving THAT into a real source_filename is a separate step
    (_start_book_reading(), via Memory Agent's resolve_document) - kept
    apart so a failed classify/extract here never risks a wrong real key
    reaching adiyan_reader's start_reading."""
    try:
        choice = await classify(text, _BOOK_READING_SKILLS, cfg['book_reading_intent'])
    except Exception as e:
        logger.error(f'Book-reading intent classification failed: {describe_exception(e)}')
        return None
    if choice.skill_id != 'start_book_reading':
        return None
    try:
        params = await extract(text, 'start_book_reading', _BookReadingRequest, cfg['extract_parameters'])
    except Exception as e:
        logger.error(f'Book-reading reference extraction failed: {describe_exception(e)}')
        return None
    # Confirmed live: a vague request ("read me a book") correctly makes
    # extract() return an empty book_reference rather than inventing a
    # title - but the caller's own `is not None` check let that empty
    # string straight through to resolve_book(''), which matched whatever
    # book happened to come first in the whole shared library (an empty
    # string is a substring of every title) and confidently started
    # reading it. Blank/whitespace-only is treated the same as "not a
    # book-reading request" here - the caller falls through to normal
    # routing instead of resolving a book nobody actually named.
    return params.book_reference if params.book_reference.strip() else None


async def _start_book_reading(
    book_reference: str, chat_id: str, from_number: Optional[str], tier: str, vertical_id: Optional[str] = None,
) -> Optional[str]:
    """Reply text for the sender, or None only on a genuinely unexpected
    failure (an agent unreachable, the calls themselves erroring) - same
    silent-on-unexpected-failure convention run()'s own except-block already
    documents. An anticipated outcome (book not found among their uploads)
    still gets a real, specific reply - the sender needs to know to upload
    the book first, not get silence.

    book_reference is the caller's own free-text wording, resolved here
    through Memory Agent's resolve_book (a real fuzzy match against their
    actual page-ingested books, mesh/memory/skills/resolve_book.py) - never
    trusted as a filename directly. Deliberately NOT resolve_document: that
    one only searches kb_documents (chunk-ingested via ingest_document),
    which has zero visibility into a book ingested via ingest_book - see
    memory_index.py's find_book_by_reference() docstring for how that gap
    was found live. Only the resolved source_filename that comes back from
    resolve_book's real lookup is ever passed to adiyan_reader's
    start_reading, same "never let the model guess a real key" rule
    mesh/adiyan_reader/skills_catalog.py's own docstring already documents
    for that skill.

    phone_number defaults to from_number (the sender's real number, already
    resolved by the WhatsApp receiver) - falls back to chat_id only if
    from_number wasn't supplied, mirroring contact_name's own `or chat_id`
    fallback elsewhere in this module."""
    memory_url = router.get_agent_url('memory')
    if memory_url is None:
        return "Knowledge Bank isn't reachable right now - try again in a moment."

    token = permissions.mint_token(chat_id, tier, vertical_id=vertical_id)
    try:
        resolved = await call_agent(memory_url, 'resolve_book', {
            'query': book_reference, 'vertical_id': vertical_id,
        }, token=token)
    except Exception as e:
        logger.error(f'Book resolution failed for {chat_id}: {e}')
        return None

    source_filename = resolved.get('source_filename') if resolved.get('found') else None

    reader_url = router.get_agent_url('adiyan_reader')
    if reader_url is None:
        return "The book reader isn't reachable right now - try again in a moment."

    if not source_filename:
        # Not among this person's own uploads - try Project Gutenberg
        # before giving up. Scoped hard to Gutenberg by
        # fetch_public_domain_book.py itself (never a general web fetch),
        # so this can only ever resolve to a verified public-domain work -
        # never something this business has no right to read aloud. A
        # miss here (not on Gutenberg either, or ingestion failed) falls
        # through to the same honest "couldn't find it" reply as before,
        # just with Gutenberg now also ruled out.
        try:
            gutenberg = await call_agent(reader_url, 'fetch_public_domain_book', {
                'query': book_reference, 'username': from_number or chat_id,
            }, token=token)
        except Exception as e:
            logger.warning(f'Gutenberg fallback failed for {chat_id}: {describe_exception(e)}')
            gutenberg = {'ingested': False}
        if gutenberg.get('ingested'):
            source_filename = gutenberg['source_filename']
        else:
            return f"I couldn't find \"{book_reference}\" among your uploaded books, or on Project Gutenberg - upload the PDF first, then ask me to read it."

    try:
        started = await call_agent(reader_url, 'start_reading', {
            'phone_number': from_number or chat_id,
            'source_filename': source_filename,
        }, token=token)
    except Exception as e:
        logger.error(f'start_reading failed for {chat_id}: {e}')
        return None

    title = source_filename.split('/', 1)[-1]
    already_active = started.get('already_active', False)

    # Confirmed live this session, twice: both a brand-new "read me this
    # book" AND a repeated one for a book already being read only ever
    # scheduled/reported a future page - even a caller who explicitly said
    # "now" got "I'll read you tonight" or "already reading you" back,
    # never an actual page. Explicitly re-asking to be read a specific
    # book - fresh or already started - reads as wanting a page right now,
    # not a second status report. Best-effort: a failure here still leaves
    # the nightly schedule correctly in place (start_reading already
    # succeeded above), so the reply degrades to a status message rather
    # than losing the whole request.
    try:
        first_page = await call_agent(reader_url, 'read_next_page', {
            'reading_job_id': started['reading_job_id'],
        }, token=token)
    except Exception as e:
        logger.error(f'Immediate page read failed for {chat_id}: {describe_exception(e)}')
        first_page = None

    # read_next_page.run() returns status='completed' for BOTH "a page was
    # actually sent" and "the book just finished, nothing left to send" -
    # only the former includes page_sent, so that's the real signal here,
    # not status alone (confirmed by reading that skill's own two return
    # statements - conflating them would have this branch claim "here's
    # page 1" on a book that just finished with zero pages sent).
    if first_page and 'page_sent' in first_page:
        lead_in = "Here's the next page of" if already_active else "Got it - here's page 1 of"
        return (
            f"{lead_in} {title} right now ({int(first_page['page_sent'])}), "
            f"and I'll keep reading you a page every night after. Next one comes at {started.get('first_reading_at', 'tonight')}."
        )
    if first_page and first_page.get('status') == 'completed':
        return f"We've already finished reading {title} together - every page has been read out. 📖"
    if already_active:
        return f"Already reading you {title} - you're on page {started.get('current_page', 0)}. Next page comes at {started.get('first_reading_at', 'tonight')}."
    return f"Got it - I'll read you {title} tonight, and every night after until it's finished. First page comes at {started.get('first_reading_at', 'tonight')}."


# Same single-skill classify-only pool shape as _BOOK_READING_SKILLS - no
# extraction step needed here (unlike start_book_reading's book_reference),
# since "who" and "which book" are both already resolved from the sender's
# own identity (mesh/adiyan_reader/skills/read_now.py's own phone_number
# lookup), never guessed from the message text.
_READ_NOW_SKILLS = [
    AgentSkill(
        id='read_now',
        name='Read Next Page Now',
        description=(
            'The caller wants their next book page read to them right now, on demand - '
            'not waiting for tonight\'s scheduled reading. Only matches an explicit '
            '"read it to me now" style request for an already-started book, not a '
            'request to start a new book (that\'s start_book_reading) or a question '
            'about how the schedule works.'
        ),
        tags=['adiyan_reader', 'book'],
        examples=[
            'Send me the next page now',
            'Read me another page right now',
            'Can I get today\'s page early',
            'Give me the next page of my book now',
        ],
        input_modes=['text/plain'],
        output_modes=['application/json'],
    ),
]


async def _resolve_read_now_request(text: str, cfg: Dict[str, Any]) -> bool:
    """True only for an explicit on-demand read request - same degrade-on-
    failure contract every other classify-based resolver in this module
    follows (a failure here just falls through to normal routing, not an
    error surfaced to the sender)."""
    try:
        choice = await classify(text, _READ_NOW_SKILLS, cfg['book_reading_intent'])
    except Exception as e:
        logger.error(f'Read-now intent classification failed: {describe_exception(e)}')
        return False
    return choice.skill_id == 'read_now'


async def _read_page_now(
    chat_id: str, from_number: Optional[str], tier: str, vertical_id: Optional[str] = None,
) -> Optional[str]:
    """Reply text, or None only on a genuinely unexpected failure - same
    convention every other action function in this module follows. Calls
    adiyan_reader's read_now directly with phone_number only
    (mesh/adiyan_reader/skills/read_now.py resolves that to the right
    reading_job_id itself, most-recently-created active job if more than
    one) - never a reading_job_id or source_filename guessed here.

    Reuses read_next_page's own result shape directly (result_summary,
    status) rather than re-deriving a reply from scratch - that skill
    already handles every real outcome (a page sent, the book finished, no
    active job at all)."""
    reader_url = router.get_agent_url('adiyan_reader')
    if reader_url is None:
        return "The book reader isn't reachable right now - try again in a moment."

    token = permissions.mint_token(chat_id, tier, vertical_id=vertical_id)
    try:
        result = await call_agent(reader_url, 'read_now', {'phone_number': from_number or chat_id}, token=token)
    except Exception as e:
        logger.error(f'read_now failed for {chat_id}: {describe_exception(e)}')
        return None

    return result.get('result_summary') or "Couldn't read a page right now - try again in a moment."


# Same single-skill classify-only pool shape as _READ_NOW_SKILLS - checked
# BEFORE _READ_NOW_SKILLS in run()'s dispatch chain (see that chain's own
# comment) since "read all" or "read page 1 to 10" would otherwise also
# plausibly match read_now's "give me the next page now" description; the
# more specific burst-read intent has to get first look.
_READ_RANGE_SKILLS = [
    AgentSkill(
        id='read_range',
        name='Read Page Range Now',
        description=(
            'The caller wants several pages read back-to-back right now, in one go - '
            'an explicit page range ("read page 1 to 10"), a relative count ("read me the '
            'next 5 pages", "send 3 more pages"), or the whole rest of the book ("read all", '
            '"read the rest", "keep going until it\'s done"). Not for a single "read the next '
            'page" request - that\'s read_now.'
        ),
        tags=['adiyan_reader', 'book'],
        examples=[
            'Read all',
            "Send me continuous voice notes till the book is complete",
            'Read page 1 to 10',
            'Read pages 5 through 20',
            'Read the rest of the book now',
            'Keep reading until the book is finished',
            'Read me the next 5 pages',
            'Send 3 more pages',
            'Give me the next 10 pages now',
        ],
        input_modes=['text/plain'],
        output_modes=['application/json'],
    ),
]


async def _resolve_read_range_request(
    text: str, cfg: Dict[str, Any],
) -> Optional[Tuple[Optional[int], Optional[int], Optional[int]]]:
    """None if this message isn't a burst-read request - same degrade-on-
    failure contract every other classify-based resolver in this module
    follows. Otherwise (start_page, end_page, page_count) - all three may
    be None (a bare 'read all'); page_count is set instead of end_page for
    a relative count ('the next 5 pages') the caller's own bookmark has to
    resolve against, which this extraction step has no visibility into -
    see read_range.py's own run() for where that resolution actually
    happens."""
    try:
        choice = await classify(text, _READ_RANGE_SKILLS, cfg['book_reading_intent'])
    except Exception as e:
        logger.error(f'Read-range intent classification failed: {describe_exception(e)}')
        return None
    if choice.skill_id != 'read_range':
        return None
    try:
        params = await extract(text, 'read_range', _ReadRangeRequest, cfg['extract_parameters'])
    except Exception as e:
        logger.error(f'Read-range extraction failed: {describe_exception(e)}')
        return (None, None, None)
    return (params.start_page, params.end_page, params.page_count)


async def _read_page_range(
    chat_id: str, from_number: Optional[str], tier: str, start_page: Optional[int], end_page: Optional[int],
    page_count: Optional[int] = None, vertical_id: Optional[str] = None,
) -> Optional[str]:
    """Reply text, or None only on a genuinely unexpected failure - same
    convention _read_page_now follows. Reuses read_range's own
    result_summary the same way _read_page_now reuses read_next_page's -
    that skill already handles every real outcome (pages sent, the book
    finished mid-range, no active job at all). page_count passes straight
    through unresolved - read_range.py's own run() is what turns it into a
    real end_page, since only it knows the caller's actual current_page."""
    reader_url = router.get_agent_url('adiyan_reader')
    if reader_url is None:
        return "The book reader isn't reachable right now - try again in a moment."

    token = permissions.mint_token(chat_id, tier, vertical_id=vertical_id)
    try:
        result = await call_agent(reader_url, 'read_range', {
            'phone_number': from_number or chat_id, 'start_page': start_page, 'end_page': end_page,
            'page_count': page_count,
        }, token=token)
    except Exception as e:
        logger.error(f'read_range failed for {chat_id}: {describe_exception(e)}')
        return None

    return result.get('result_summary') or "Couldn't read those pages right now - try again in a moment."


async def _ingest_into_knowledge_base(
    media: Dict[str, Any], chat_id: str, tier: str, contact_name: Optional[str], do_ocr: bool = True,
    vertical_id: Optional[str] = None, visibility: str = 'private',
) -> Tuple[Optional[str], Optional[str]]:
    """(reply, source_filename). reply is None only for a genuinely
    unexpected failure (see this module's own silent-on-failure convention,
    same reasoning as run()'s own except-block) - an anticipated, clearly-
    worded outcome (storage unavailable, Docling couldn't read the file)
    still gets a real reply, since this is a coach-initiated action they
    need feedback on. source_filename is None whenever ingestion didn't
    actually succeed, and set to Memory Agent's own resolved key on success
    - the caller needs this to run analysis directly against the exact
    document just ingested, without a separate resolution step.

    Calls Memory Agent's ingest_document skill directly via a structured
    DataPart (mesh/lib/a2a_client.py's call_agent, not call_agent_with_text)
    - the caller already knows exactly what it wants, same reasoning
    call_agent() itself documents. Not resolved through router.py's
    classify pool (get_agent_url(), not route_to_agent()) - see
    mesh/memory/skills/ingest.py's own docstring for why ingest_document
    isn't in that pool at all.

    media is either the resolved `document` param (a real WhatsApp file
    upload, already carrying a real filename) or the resolved `image` param
    (a photo/screenshot vision.py classified as knowledge content, which
    WhatsApp never gives a filename for) - one synthesized here so Docling
    still has an extension to format-sniff against.

    username (passed to Memory Agent as who's storing this - see
    memory_index.py's ingest_document docstring on why it folds into storage,
    not just an access-control label) is contact_name when WhatsApp gave
    one, falling back to the raw chat_id - the same 'Unknown' fallback
    openwa_receiver.py already applies means contact_name is effectively
    always populated in practice.

    owner_identity is deliberately chat_id, not username above - a display
    name can be arbitrary or reused (see memory_index.py's own
    _scope_filters() docstring), but chat_id is the same stable identity
    permissions.mint_token() already uses for this exact sender. `visibility`
    defaults to 'private' (memory_index.py's own default) - visible only to
    whoever uploaded it, and the owner. The caller passes 'global' instead
    when _resolve_visibility_preference() found the owner's own caption
    explicitly asking for it (e.g. "make this available for every
    customer") - see that function's own docstring for why this is
    owner-only, never something a customer's own caption can trigger.

    do_ocr defaults True (Docling's own default, safe for anything that
    might be scanned) - the caller only passes False once
    _resolve_ocr_preference() has confirmed the uploader's own caption
    explicitly said this document is scan-free. See memory_index.py's
    _pdf_converter() docstring for why that confirmation matters: a wrong
    guess here means genuinely scanned pages come back silently blank
    instead of OCR'd."""
    memory_url = router.get_agent_url('memory')
    if memory_url is None:
        return "Knowledge Bank isn't reachable right now - try again in a moment.", None

    mimetype = media.get('mimetype') or 'application/octet-stream'
    filename = media.get('filename')
    if not filename:
        extension = 'pdf' if mimetype == 'application/pdf' else mimetype.split('/')[-1]
        filename = f'whatsapp_upload.{extension}'

    token = permissions.mint_token(chat_id, tier, vertical_id=vertical_id)
    try:
        result = await call_agent(memory_url, 'ingest_document', {
            'content_b64': media['data'],
            'filename': filename,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'mimetype': mimetype,
            'username': contact_name or chat_id,
            'owner_identity': chat_id,
            'do_ocr': do_ocr,
            'visibility': visibility,
        }, token=token)
    except Exception as e:
        logger.error(f'Ingestion failed for {chat_id}: {e}')
        return None, None

    if not result.get('available', True):
        return "Knowledge Bank isn't available right now (its storage backend is unreachable) - try again later.", None
    if not result.get('ingested'):
        # result['error'] is only ever a deliberate, user-actionable message
        # (see mesh/memory/skills/ingest.py's own ValueError/Exception split)
        # - never a raw internal exception. None here means something
        # internal failed; the real reason is already logged there, not
        # something to guess a WhatsApp-safe phrasing for by dumping it here.
        return result.get('error') or "Couldn't add that to the knowledge base right now - try again in a moment.", None
    # int(...): A2A's Part.data travels through a protobuf Struct, which has
    # no integer type, only double - confirmed live that the real int
    # ingest.py returns for 'chunks' silently became 1.0 by the time it got
    # here, printing "1.0 chunk(s) indexed" in an actual WhatsApp reply.
    visibility_note = ' - available to every customer' if visibility == 'global' else ''
    reply = f"Added to the knowledge base ({int(result['chunks'])} chunk(s) indexed){visibility_note}."

    # Also page-ingest every upload - confirmed live this session that
    # ingest_document alone leaves a document completely invisible to
    # AdiyanReader (ingest_document/ingest_book write to two entirely
    # separate stores, see mesh/memory/skills/ingest_book.py's own
    # docstring), so a WhatsApp upload used to never actually become
    # readable as a nightly book. Best-effort and silent on failure here -
    # ingest_book.run() already degrades gracefully for anything that isn't
    # genuinely paginated (a screenshot, a slide deck), and a book someone
    # never asks to be read shouldn't cost them a confusing extra reply
    # about page-ingestion succeeding or failing.
    try:
        book_result = await call_agent(memory_url, 'ingest_book', {
            'content_b64': media['data'],
            'filename': filename,
            'username': contact_name or chat_id,
            'do_ocr': do_ocr,
        }, token=token)
        if book_result.get('ingested'):
            reply += f" Ready to be read aloud too ({int(book_result['num_pages'])} pages)."
    except Exception as e:
        logger.warning(f'Page-ingestion failed for {chat_id}: {e}')

    return reply, result.get('source_filename')


async def _resolve_ocr_preference(caption: str, cfg: Dict[str, Any]) -> bool:
    """False (the safe default - OCR stays on) unless the caption itself
    explicitly says this upload is plain text with nothing to scan. Same
    degrade-on-failure contract every other resolver in this module
    follows: an extraction failure here must never accidentally skip OCR
    on a document that actually needed it, so it fails toward doing more
    work, not less.

    Deliberately a separate extract() call, not a field bolted onto
    _resolve_upload_instruction()'s existing classify() - that one only
    ever answers "is this caption an analysis instruction," a plain
    skill_id match with no structured fields of its own to extend.

    This is genuinely worth its own small model call rather than a vector-
    similarity shortcut against a set of example phrasings: the whole
    point of this flag is telling apart phrasings that share the same
    vocabulary but mean opposite things ("no image scan needed" vs "scan
    the images carefully, it has photos") - exactly the negation case this
    session's own staleness research found embeddings handle badly
    (cos("teal", "not teal") = 0.86, nearly as close as the term itself).
    An extraction model reads the whole sentence's grammar instead of
    just measuring vocabulary overlap, which is what this decision
    actually turns on."""
    if not caption or not caption.strip():
        return False
    try:
        params = await extract(caption, 'upload_ocr_preference', _UploadOcrPreference, cfg)
    except Exception as e:
        logger.error(f'OCR-preference extraction failed: {describe_exception(e)}')
        return False
    return params.skip_image_scanning


async def _resolve_visibility_preference(caption: str, cfg: Dict[str, Any]) -> bool:
    """False (the safe default - stays private, visible only to whoever
    uploaded it and the owner) unless the caption itself explicitly says
    this document should be available to every customer. Same degrade-on-
    failure contract as _resolve_ocr_preference: an extraction failure here
    must never accidentally make a document global that wasn't meant to be
    - a private document mistakenly staying private is recoverable (ask
    again, or the owner sets it global via config_agent later); a document
    that should have stayed private going global by mistake is a real data
    leak across every one of a business's customers, not a small thing to
    get wrong in the other direction.

    Owner-only by construction, not by a check in this function - see this
    module's own call site, which only ever calls this for tier == 'owner'.
    A customer's own upload always defaults to private no matter what their
    caption says; only the business owner can decide something goes out to
    every customer, same reasoning update_customer_record.py's
    owner_section split already documents for a consequential fact."""
    if not caption or not caption.strip():
        return False
    try:
        params = await extract(caption, 'upload_visibility_preference', _UploadVisibilityPreference, cfg)
    except Exception as e:
        logger.error(f'Visibility-preference extraction failed: {describe_exception(e)}')
        return False
    return params.global_visibility


async def _resolve_vertical_spec_intent(caption: str, cfg: Dict[str, Any]) -> bool:
    """True only if the caption explicitly says this upload is a business/
    vertical spec to apply - False (falls through to normal knowledge-base
    ingestion) for an empty caption or any classification failure, same
    degrade-toward-the-safer-default contract as _resolve_ocr_preference:
    misreading an ordinary document as a vertical spec would try to parse
    it as YAML and fail loudly; misreading a real spec as an ordinary
    document just files it away, recoverable by asking again with a
    clearer caption."""
    if not caption or not caption.strip():
        return False
    try:
        choice = await classify(caption, _VERTICAL_SPEC_SKILLS, cfg)
    except Exception as e:
        logger.error(f'Vertical-spec intent classification failed: {describe_exception(e)}')
        return False
    return choice.skill_id == 'apply_vertical_spec'


async def _apply_vertical_spec_upload(media: Dict[str, Any], chat_id: str, tier: str) -> Optional[str]:
    """Reply text, or None only for a genuinely unexpected failure (Config
    Agent unreachable, the call itself erroring) - same silent-on-failure
    convention as _ingest_into_knowledge_base. A rejected/invalid spec
    still gets a real, specific reply (apply_vertical_spec.run()'s own
    'error' field), since the owner needs to know exactly what was wrong
    to fix their spec, not silence.

    Deliberately bypasses _ingest_into_knowledge_base entirely - a vertical
    spec is Adiyan's own configuration, never something that should also
    land in the searchable knowledge base or the nightly-reading pages
    store the way an ordinary upload does."""
    config_url = router.get_agent_url('config_agent')
    if config_url is None:
        return "Config Agent isn't reachable right now - try again in a moment."

    filename = media.get('filename') or 'vertical-spec.yaml'
    token = permissions.mint_token(chat_id, tier)
    try:
        result = await call_agent(config_url, 'apply_vertical_spec', {
            'content_b64': media['data'], 'filename': filename,
        }, token=token)
    except Exception as e:
        logger.error(f'apply_vertical_spec failed for {chat_id}: {e}')
        return None

    if not result.get('applied'):
        return f"Couldn't apply that spec: {result.get('error') or 'unknown reason'}"
    if not result.get('activated'):
        return (
            f"Applied {result.get('vertical_id')!r} but couldn't activate it: "
            f"{result.get('error') or 'unknown reason'}"
        )
    business_name = result.get('business_name') or result.get('vertical_id')
    written = result.get('written') or []
    return (
        f"Applied and activated {business_name!r} ({len(written)} setting(s) updated). "
        "Your customers will see this persona starting now - your own messages are unaffected."
    )


async def _resolve_upload_instruction(caption: str, cfg: Dict[str, Any]) -> Optional[str]:
    """None if caption is just a label, not an instruction - the caller
    then does a plain ingest-only reply, same as before this existed.
    Otherwise returns caption itself, ready to hand straight to Analysis
    Agent as its instruction param. Degrades to None (ingest-only) on any
    classification failure - a caption misclassified as "just a label"
    costs nothing the user would notice; failing this whole branch would
    cost the ingest confirmation they're actually waiting for."""
    if not caption or not caption.strip():
        return None
    try:
        choice = await classify(caption, _UPLOAD_INSTRUCTION_SKILLS, cfg)
    except Exception as e:
        logger.error(f'Caption-intent classification failed: {describe_exception(e)}')
        return None
    return caption if choice.skill_id == 'analyze_upload' else None


async def _run_analysis(
    instruction: str, source_filename: str, chat_id: str, tier: str, vertical_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """None on any failure (Analysis Agent unreachable, the call itself
    erroring) - the caller falls back to the plain ingest confirmation
    rather than surfacing a raw error, same silent-on-unexpected-failure
    convention as the rest of this module. Calls Analysis Agent's
    analyse_this directly with source_filename already known - no
    resolution step needed here, unlike the free-text follow-up case
    (a later message naming the document by topic), which goes through
    router.py's normal classify pool and lets analyse_this's own
    resolve_document call handle that fuzzy match instead."""
    analysis_url = router.get_agent_url('analysis')
    if analysis_url is None:
        logger.error('Analysis Agent not present in the current agent pool')
        return None

    token = permissions.mint_token(chat_id, tier, vertical_id=vertical_id)
    try:
        return await call_agent(analysis_url, 'analyse_this', {
            'instruction': instruction,
            'source_filename': source_filename,
        }, token=token)
    except Exception as e:
        logger.error(f'Analysis failed for {chat_id}: {e}')
        return None


async def _resolve_image_intent(text: str, image: Optional[Dict[str, Any]]) -> tuple:
    """(text, kb_pending) - text unchanged unless the image turned out to be
    a contact card, in which case it becomes a caption fed into the exact
    same gate below a typed admin command goes through (no separate
    registration logic here). kb_pending is True for an image vision.py
    classified as knowledge content - handled as its own branch in run()
    rather than silently falling through to normal routing. A real document
    upload (the `document` param, not `image`) never goes through this
    function at all - see run()'s own comment on why."""
    if image is None:
        return text, False

    purpose = await vision.classify_image(image['data'], image['mimetype'])
    if purpose == 'contact_card':
        caption = await vision.describe_contact_image(image['data'], image['mimetype'])
        return (caption or text), False
    if purpose == 'knowledge_content':
        return text, True
    return text, False


async def run(
    text: str,
    chat_id: str,
    contact_name: Optional[str] = None,
    from_number: Optional[str] = None,
    image: Optional[Dict[str, Any]] = None,
    document: Optional[Dict[str, Any]] = None,
    audio: Optional[Dict[str, Any]] = None,
    location: Optional[Dict[str, Any]] = None,
    is_self_chat: bool = False,
) -> Dict[str, Any]:
    # Mongo-backed via mesh/lib/config_sdk.py (pilot agent for the central
    # config SDK) - local runtime_config.json/constants.py are now only the
    # fallback/first-seed defaults, not the source of truth. See
    # config_sdk.py's own docstring for the auto-seed/degrade-gracefully
    # behavior.
    cfg = await config_sdk.load_stage_configs(AGENT_ID, load_runtime_config(AGENT_CODE_DIR))
    whatsapp_mcp_url = await config_sdk.get_constant(
        AGENT_ID, 'whatsapp_mcp_url', WHATSAPP_MCP_URL,
        description='URL of the WhatsApp MCP server used to actually send the reply back to the sender.',
    )

    # A shared location carries no body text of its own (same as a photo or
    # voice note) - folded into `text` here, same principle as
    # transcribe_audio() below, so a customer sharing their location never
    # silently vanishes with nothing for the rest of this pipeline (rules_
    # engine's own gate, routing, the ReAct loop) to see. Only when there's
    # no caption already - a location sent WITH a typed message (e.g. "here's
    # @adiyan where to deliver") keeps that text untouched, the coordinates
    # just aren't described a second time in it.
    if location is not None and not text.strip():
        parts = [p for p in (location.get('description'), location.get('address')) if p]
        where = ' - '.join(parts) if parts else f"{location.get('latitude')}, {location.get('longitude')}"
        text = f'[shared a location: {where}]'

    text, kb_pending = await _resolve_image_intent(text, image)
    if document is not None:
        # A real document upload's purpose is never ambiguous the way an
        # image's is (vision.classify_image has to tell a contact-card
        # photo apart from actual knowledge content) - it always means "add
        # this to the knowledge base," so it skips classification entirely
        # and goes straight into the same kb_pending branch below.
        kb_pending = True

    # A voice note's transcribed text REPLACES `text` (which openwa_mcp
    # never populates for pure audio - see server.py's own webhook
    # handler), before the gate check below - the summon-phrase/
    # registration logic has to see what was actually said, same as any
    # typed message. Transcription failure (whisper missing, timed out, or
    # genuinely produced nothing) leaves `text` empty, which falls through
    # to the same silent-stranger/no-command handling an empty typed
    # message would already get - no separate error path needed.
    audio_pending = False
    detected_language = None
    if audio is not None:
        transcribed = await transcribe_audio(base64.b64decode(audio['data']))
        if transcribed:
            text = transcribed.text
            detected_language = transcribed.language
            audio_pending = True
            # No spoken audio can ever produce a literal "@" character, so
            # the typed gate check ('@adiyan' in text.lower()) can NEVER
            # pass for a voice note, in ANY language - confirmed live this
            # session across Tamil, Hindi, and English voice notes, none
            # ever transcribing an "@". An exact-string list of known
            # spellings (this block's own predecessor) was tried first and
            # abandoned after three fresh real voice notes each produced a
            # brand-new spelling never seen before ('अडियान', 'அரியான்'
            # "Ariyan", 'அடியான்') - "Adiyan" isn't a real word in Tamil or
            # Hindi, so Whisper has no dictionary entry to anchor to and
            # approximates the sound differently each time, making exact
            # matching an unbounded, always-behind chase. This instead
            # transliterates each word to Latin script (auto-detecting
            # source script via indic_transliteration) and fuzzy-matches
            # it against "adiyan" (rapidfuzz) - see
            # _word_matches_spoken_summon()'s own docstring for the real
            # scores that set its threshold. Every matching word is
            # stripped, not just the first - confirmed live this session
            # that Whisper can transcribe more than one wake-word-like
            # token in a single clip (a real clip came back as 'அடியேன்
            # அடியான் சென்னி மைல் பாண் நம்பர்' - 'அடியேன்', a Tamil
            # honorific one vowel-sign away from 'அடியான்', fuzzy-matches
            # just as well). Breaking after the first match left the
            # second one sitting in the routed text/query as noise. The
            # real summon phrase is folded in once regardless of how many
            # matched (strip_summon_phrase removes it again further down,
            # same as it would for a typed "@adiyan"), rather than
            # teaching rules_engine.check() itself about audio-only
            # phrasing.
            summoned_word_matched = False
            for word in text.split():
                if _word_matches_spoken_summon(word):
                    text = re.sub(re.escape(word), '', text, count=1).strip()
                    summoned_word_matched = True
            if summoned_word_matched:
                text = f'{rules_engine.DEFAULT_SUMMON_PHRASE} {text}'
        else:
            # Confirmed live this session: `text` was left at whatever
            # message_body already was on a failed transcription - for a
            # raw audio FILE attachment (WhatsApp's own 'audio' kind, as
            # opposed to a recorded 'ptt' voice memo), that's frequently
            # the literal OS filename WhatsApp auto-populates as a
            # caption ("WhatsApp Audio 2026-09-14 at 11.23.31.opus"),
            # which then reached a real classify_skill call as if it were
            # the actual request. Cleared here so a failed transcription
            # falls through to the same silent-stranger/no-command
            # handling an empty typed message already gets, instead of
            # classifying meaningless leftover text.
            logger.warning(f'Voice note transcription failed or produced nothing for {chat_id}')
            text = ''

    # Threaded into every humanize() call below - the underlying skill's
    # raw result is grounded in whatever language its own source documents/
    # tools use (English, in practice), but a voice note's sender asked in
    # their own spoken language and should get a reply back in that same
    # language, not a mismatch. Only set for audio - a typed message's own
    # language is left alone, same reasoning humanize()'s own docstring
    # documents.
    #
    # Previously hardcoded to 'Tamil' unconditionally for every voice note
    # regardless of what was actually spoken - confirmed live this session
    # that this was wrong the moment a real non-Tamil voice note came in;
    # it would have forced a Tamil reply onto an English or Hindi speaker.
    # detected_language is Whisper's own language detection (the same
    # detection that already has to run correctly to produce `text` in the
    # first place - see audio_transcribe.py's own TranscriptionResult
    # docstring for why this was being discarded before), mapped to a
    # display name a natural-language prompt instruction can actually use
    # ('ta' -> 'Tamil'). None (no override at all, same as a typed message)
    # if detection came back empty/unrecognized - degrading to "let the
    # model pick" is safer than guessing a specific wrong language.
    reply_language = language_display_name(detected_language) if audio_pending else None

    conn = db.connect(state_db_path(AGENT_ID))
    gate_reply, tier, vertical_id, just_auto_registered = await rules_engine.check(
        conn, chat_id, contact_name, text, from_number, cfg['add_named_contact'],
        is_self_chat=is_self_chat,
    )
    if gate_reply is None and tier is None:
        # Unregistered stranger, no register/unregister command - stay
        # completely silent (see rules_engine.check()'s docstring). Never
        # reaches send_message at all, unlike every other branch below.
        return {'chat_id': chat_id, 'reply': None, 'delivered': False}

    # Orchestrator's OWN persona (read by this process's own ask() calls,
    # e.g. humanize() below) was already resolved once by bootstrap.py's
    # wrapper - before this function had even parsed the incoming text, so
    # before rules_engine.check() could determine which vertical (if any)
    # actually applies. Re-set here with the now-known answer, overriding
    # that earlier guess for the rest of THIS request - contextvars support
    # exactly this (a later .set() in the same task is what every later
    # .get() in it sees). Skipped for the owner: persona_hook.py's own
    # resolve_persona_context() already forces '' for is_owner regardless of
    # vertical_id, so there's nothing to correct.
    if tier != 'owner':
        persona_hook.CURRENT_PERSONA_CONTEXT.set(
            await persona_hook.resolve_persona_context(AGENT_ID, {'tier': tier, 'vertical_id': vertical_id})
        )

    # Strip whichever phrase actually matched - vertical_id is None for the
    # platform default, or a specific vertical whose OWN phrase won instead
    # (see rules_engine.check()'s own docstring on why "which vertical
    # applies" is now resolved per-message, not read from one deployment-
    # wide toggle). Every mint_token() call below carries this same
    # vertical_id, which is what lets two verticals' messages get correctly
    # separated even while both are live on this one deployment.
    summon_phrase = await config_sdk.get_constant(
        AGENT_ID, 'summon_phrase', rules_engine.DEFAULT_SUMMON_PHRASE,
        vertical_id=vertical_id or config_sdk.PLATFORM_VERTICAL,
    )
    text = rules_engine.strip_summon_phrase(text, summon_phrase)

    # POC: a sender can opt this one message into compute_share's
    # peer-sharing network (mesh/lib/agent_sdk.py's ask() `community`
    # param) by including a trigger word anywhere in their own text - the
    # WORD itself is configurable (dashboard-editable via config_sdk, not
    # hardcoded 'communitySearch' here), but what it maps to internally is
    # always the fixed sentinel ask()/Inference Router actually check for.
    # Stripped from `text` before routing/classification for the same
    # reason the @Adiyan mention is above - it's a signal about how to
    # answer, not part of the question itself, and left in it's just noise
    # for the skill classifier and downstream agents to ignore.
    trigger_word = await config_sdk.get_constant(
        AGENT_ID, 'community_search_trigger_word', 'communitySearch',
        description="Word a sender includes anywhere in their message to opt this one reply into compute_share's peer-sharing network.",
    )
    community = None
    if trigger_word and trigger_word.lower() in text.lower():
        text = re.sub(re.escape(trigger_word), '', text, flags=re.IGNORECASE).strip()
        community = 'communitySearch'

    pending_document = None
    pending_image = None
    # True only for a genuine routed conversation exchange (the else branch
    # below) - not a registration/unregistration command, not a document
    # upload. Those aren't "what did we talk about" content, and conflating
    # them would mean Memory Agent's conversation store filling up with
    # admin bookkeeping instead of things actually worth recalling later.
    should_remember = False
    if gate_reply is not None:
        # Registration, unregistration, or an unregistered-sender rejection
        # already fully handled it - never reaches routing.
        reply = gate_reply
        if gate_reply == rules_engine.REGISTERED_REPLY and _LOGO_B64 is not None:
            # A fresh registration gets the logo alongside the welcome text,
            # sent as one message via send_image's own caption field - not
            # a separate text send followed by a separate image send.
            # send_image, not send_document - confirmed live send_document
            # hangs until it times out for an actual image mimetype.
            pending_image = {
                'filename': 'adiyan_logo.png', 'content_b64': _LOGO_B64, 'mimetype': 'image/png',
            }
    elif kb_pending:
        # Only reachable once the gate above has already let this sender
        # through (owner or a registered client) - a stranger's image or
        # document still gets the normal not-registered rejection, same as
        # any other message from them. tier is never None here, same
        # reasoning the routing branch below already documents for itself.
        # Checked BEFORE anything else in this branch, and only for the
        # owner - a vertical spec must never be silently swallowed into the
        # searchable knowledge base the way an ordinary document would be.
        # See _VERTICAL_SPEC_SKILLS's own comment for why this is never
        # even attempted for a customer upload.
        #
        # is_yaml_upload checked first, and OR'd with the caption classify
        # rather than replacing it - confirmed live this session that a
        # real caption ("here is my coaching business details") reads as an
        # ordinary document label to a classifier with no reason to think
        # otherwise, and doesn't even mention "spec"/"apply"/"vertical" -
        # the classify alone will keep missing genuine specs phrased that
        # naturally. A .yaml/.yml filename is a much stronger, free signal:
        # Docling cannot parse YAML as a document at all (the exact
        # "Conversion failed... File format not allowed" error that same
        # real upload hit, going to the normal ingestion path), so an
        # upload with that extension was never going to succeed as an
        # ordinary document anyway - there is no real downside to treating
        # it as a spec attempt first.
        media_for_upload = document or image
        filename = (media_for_upload or {}).get('filename') or ''
        is_yaml_upload = filename.lower().endswith(('.yaml', '.yml'))
        if tier == 'owner' and (is_yaml_upload or await _resolve_vertical_spec_intent(text, cfg['caption_intent'])):
            reply = await _apply_vertical_spec_upload(media_for_upload, chat_id, tier)
        else:
            # Resolved from the caption BEFORE ingestion, not after - once
            # Docling has already spent 30-50s/page skipping OCR it thought
            # it needed, there's no undoing that cost. See
            # _resolve_ocr_preference()'s own docstring for why this is a
            # separate extract() call rather than a field on the classify()
            # right below (_resolve_upload_instruction), and why it
            # defaults to False (OCR stays on) on any failure.
            skip_image_scanning = await _resolve_ocr_preference(text, cfg['extract_parameters'])
            # Owner-only, same gate as _apply_vertical_spec_upload's own
            # `tier == 'owner'` check just above - a customer's own caption
            # can never make their upload visible to every other customer,
            # no matter what it says. See _resolve_visibility_preference's
            # own docstring for why this defaults to private on any
            # ambiguity or failure.
            visibility = 'private'
            if tier == 'owner' and await _resolve_visibility_preference(text, cfg['extract_parameters']):
                visibility = 'global'
            ingest_reply, source_filename = await _ingest_into_knowledge_base(
                media_for_upload, chat_id, tier, contact_name, do_ocr=not skip_image_scanning,
                vertical_id=vertical_id, visibility=visibility,
            )
            if ingest_reply is None or source_filename is None:
                # Either ingestion itself failed outright (source_filename
                # is also None in that case), or it succeeded without a
                # resolvable source_filename (shouldn't happen given
                # ingest_document's own contract, but analysis has nothing
                # to run against either way) - the plain ingest outcome
                # (possibly None, going silent) is already the right reply,
                # nothing to add.
                reply = ingest_reply
            else:
                # The caption ("analyse this presentation and share the
                # mistakes you find") might be an actual instruction, not
                # just a label - see _resolve_upload_instruction()'s own
                # docstring. Only checked once ingestion has already
                # succeeded: nothing to analyze if the document was never
                # actually stored.
                instruction = await _resolve_upload_instruction(text, cfg['caption_intent'])
                if instruction is None:
                    reply = ingest_reply
                else:
                    analysis = await _run_analysis(instruction, source_filename, chat_id, tier, vertical_id=vertical_id)
                    if analysis is None:
                        # Analysis failed - still confirm the ingest, which
                        # did genuinely succeed, rather than losing that
                        # feedback too.
                        reply = ingest_reply
                    elif analysis.get('content_b64'):
                        pending_document = analysis
                        caption_source = {k: v for k, v in analysis.items() if k != 'content_b64'}
                        reply = await humanize(text, caption_source, cfg['humanize'], community=community, language=reply_language, is_owner=tier == 'owner', vertical_id=vertical_id)
                    else:
                        reply = analysis.get('result') or ingest_reply
    elif (pending_book := await _resolve_pending_book_disambiguation(text, contact_name, chat_id, vertical_id, cfg)) is not None:
        # Checked FIRST among the book-action branches - a bare follow-up
        # like "for all" or "the crime one" matches none of the classifiers
        # below on its own, so it has to be recognized as the answer to the
        # "which book?" question we just asked before they get a chance to
        # reject it. See _resolve_pending_book_disambiguation()'s own docstring.
        should_remember = True
        pending_action, pending_request = pending_book
        reply = await pending_action(pending_request, chat_id, from_number, tier, vertical_id=vertical_id)
    elif await _resolve_voice_samples_request(text, cfg):
        # Checked BEFORE _resolve_book_reading_request and _CHANGE_VOICE_SKILLS
        # - "what voices do you have" must never be classified as a request
        # to start a book named that, or as already naming a specific voice.
        should_remember = True
        reply = await _send_voice_samples(chat_id, from_number, tier, vertical_id=vertical_id)
    elif (change_voice_request := await _resolve_change_voice_request(text, cfg)) is not None:
        # Checked BEFORE _resolve_book_reading_request too, same reasoning.
        should_remember = True
        reply = await _change_voice(change_voice_request, chat_id, from_number, tier, vertical_id=vertical_id)
    elif (reread_request := await _resolve_reread_page_request(text, cfg)) is not None:
        # Checked BEFORE _resolve_book_reading_request - see _REREAD_SKILLS'
        # own comment for why "read that again"/"slower" must never be
        # classified as a request to start a new book.
        should_remember = True
        reply = await _reread_page(reread_request, chat_id, from_number, tier, vertical_id=vertical_id)
    elif (restart_request := await _resolve_restart_reading_request(text, cfg)) is not None:
        # Checked BEFORE _resolve_book_reading_request - see
        # _RESTART_READING_SKILLS' own comment for why "start all over
        # again" must never be classified as a request to start a NEW book
        # (it isn't a book title) - a real subscriber hit exactly this gap.
        should_remember = True
        reply = await _restart_book_reading(restart_request, chat_id, from_number, tier, vertical_id=vertical_id)
    elif (stop_request := await _resolve_stop_reading_request(text, cfg)) is not None:
        # Checked BEFORE _resolve_book_reading_request - see
        # _STOP_READING_SKILLS' own comment for why a "stop reading X" must
        # never fall through to being classified as a request to start one.
        should_remember = True
        reply = await _stop_book_reading(stop_request, chat_id, from_number, tier, vertical_id=vertical_id)
    elif (book_reference := await _resolve_book_reading_request(text, cfg)) is not None:
        # Only reached once the gate has already let this sender through and
        # there's no image/document attached - a stranger or an upload
        # caption never gets here, same reasoning kb_pending's own comment
        # documents. Lazily evaluated (only classified when the branches
        # above didn't already claim this message) - no wasted LLM call on
        # every upload or gate-handled message.
        #
        # should_remember=True here (and in the two read_range/read_now
        # branches below) - previously left unset, so no book-reading
        # exchange ever reached mem0 at all. Confirmed live this was the
        # actual root cause of a real memory bug: a stale fact from some
        # earlier, unrelated exchange ("User requested to have 'The Power
        # of Now' read to them") was the ONLY book-shaped memory this
        # contact had, because nothing about their real, ongoing reading
        # activity was ever being written to compete with or update it -
        # working around that gap by avoiding memory entirely (routing
        # "no title named" through adiyan_reader's own deterministic active-
        # job lookup instead) fixed the immediate symptom but left the
        # underlying memory blind spot in place. Recording the real
        # exchange here is the actual fix: memory now reflects what's
        # genuinely happening, not just whatever happened to slip in
        # through the generic fallback path once.
        should_remember = True
        reply = await _start_book_reading(book_reference, chat_id, from_number, tier, vertical_id=vertical_id)
    elif (range_pages := await _resolve_read_range_request(text, cfg)) is not None:
        # Checked BEFORE _resolve_read_now_request - "read all" or "read
        # page 1 to 10" would otherwise also plausibly match read_now's own
        # single-next-page description. Same lazy-evaluation reasoning as
        # the book-reading branch above.
        should_remember = True
        reply = await _read_page_range(
            chat_id, from_number, tier, range_pages[0], range_pages[1], page_count=range_pages[2],
            vertical_id=vertical_id,
        )
    elif await _resolve_read_now_request(text, cfg):
        # Same lazy-evaluation reasoning as the book-reading branch above -
        # only classified once nothing earlier in this chain already
        # claimed the message.
        should_remember = True
        reply = await _read_page_now(chat_id, from_number, tier, vertical_id=vertical_id)
    elif community == 'communitySearch':
        # The sender explicitly asked to skip this machine entirely, not
        # just the final reply-wording step - route_to_agent()'s own
        # classify() and Analysis Agent's ReAct loop are BOTH local-only
        # (schema/tool-bound ask() calls, never offloadable, see
        # agent_sdk.py's own docstring), so the normal routing path below
        # would still hit local Ollama at least twice before ever
        # reaching a humanize() call that could actually offload -
        # useless, and actively harmful, on a machine the sender is
        # explicitly saying not to burden (confirmed live: this is
        # exactly the case that left a constrained machine unusable).
        # Going straight to a plain-text ask() call here skips routing
        # and Analysis Agent's reasoning entirely - a real, deliberate
        # quality tradeoff (no skill routing, no document/memory tools),
        # not a bug: the sender asked for this machine to be left alone,
        # not for a lesser version of the normal answer.
        should_remember = True
        try:
            reply = await _agent.ask(text, stage='community_direct', community=community)
        except Exception as e:
            # Same silent-on-unexpected-failure convention as the routing
            # branch below (see its own comment on why: a raw exception
            # must never reach WhatsApp) - here specifically covers local
            # Ollama unreachable AND no peer available either, the one
            # case complete.py itself can't paper over.
            logger.error(f'Direct community ask failed for {chat_id}: {describe_exception(e)}')
            reply = None
    else:
        should_remember = True
        try:
            # tier is never None here - the only way to reach this branch is
            # gate_reply being None, and rules_engine.check() only returns a
            # None tier alongside a set reply (the not-registered rejection).
            token = permissions.mint_token(chat_id, tier, vertical_id=vertical_id)

            # Short-term chat history, prepended only for the two forward
            # calls below - NOT for route_to_agent()'s own classify() call
            # just below, which compares text against short skill
            # description/example phrases and would only get confused by a
            # history block glued onto it. Confirmed live: "I really enjoy
            # trekking in the Himalayas" -> ack -> "What gear should I pack
            # for it?" got "which activity?" back instead of resolving "it" -
            # chat_cache.get_recent_turns() had a real answer sitting right
            # there the whole time, nothing ever read it. See
            # chat_cache.format_recent_turns()'s own docstring for the
            # relevance-filtering (not full-window) behavior.
            history = await chat_cache.format_recent_turns(
                contact_name or chat_id, text, cfg['filter_chat_history'], vertical_id=vertical_id,
            )

            # Long-term memory, fetched unconditionally - the same class of
            # bug chat_cache's own history fix (above) already closed for
            # short-term context, now closed for long-term facts too.
            # recall_contact_memory existed as a tool Analysis Agent's ReAct
            # loop could choose to call, but a choice isn't a guarantee: "I'm
            # vegetarian" logged via remember_interaction, then "give me a
            # recipe" later, had no reliable path back to that fact - it
            # depended on the model happening to decide this instruction
            # warranted a memory lookup, which recall_contact_memory's own
            # docstring never promised ("not for using this history to
            # reason or advise"). mem0_backend.retrieve() is a plain vector
            # search, not an LLM call - cheap enough to run on every message,
            # the same reasoning chat_cache's own relevance filter already
            # relies on. A service token, not the sender's own tier - this
            # is Orchestrator fetching context to compose the reply, not the
            # sender exercising a skill, same reasoning is_owner()'s own
            # internal lookup already documents for itself. Silent on any
            # failure (Memory unreachable, mem0 unavailable, nothing on file
            # yet) - a missing preference degrades to today's behavior, it
            # never blocks or garbles the reply.
            memory_context = ''
            try:
                memory_url = router.get_agent_url('memory')
                if memory_url is not None:
                    service_token = permissions.mint_token('orchestrator', 'service')
                    recall_result = await call_agent(memory_url, 'recall_contact_memory', {
                        'contact_name': contact_name or chat_id, 'query': text, 'top_k': 3,
                        'vertical_id': vertical_id,
                    }, token=service_token)
                    snippets = recall_result.get('snippets') or []
                    if snippets:
                        memory_context = 'What you know about this person:\n' + '\n'.join(f'- {s}' for s in snippets)
            except Exception as e:
                logger.warning(f'Long-term memory recall failed for {chat_id}: {describe_exception(e)}')

            # Assembled in a fixed order (long-term facts, then recent
            # turns, then the message itself) regardless of which pieces
            # are actually present, so the model always sees the same
            # structure rather than a shape that varies message to message.
            context_blocks = [b for b in (memory_context, history) if b]
            if context_blocks:
                augmented_text = '\n\n'.join(context_blocks) + f'\n\nNew message: {text}'
            else:
                augmented_text = text

            async def _fallback_to_analysis() -> str:
                # No skill classified this (or the agent route_to_agent()
                # picked rejected it on its own reclassification - see the
                # except clause below) - Analysis Agent is the fallback,
                # not a canned "I don't know" reply. Called directly via a
                # structured DataPart (skill_id already known - there's
                # exactly one skill on this agent), same reasoning
                # _run_analysis() already documents for the upload+instruct
                # combined flow, not routed through classify() again.
                analysis_url = router.get_agent_url('analysis')
                if analysis_url is None:
                    return "Sorry, I'm not sure how to help with that yet."
                result = await call_agent(analysis_url, 'analyse_this', {
                    'instruction': augmented_text,
                    'contact_name': contact_name,
                }, token=token)
                if result.get('content_b64'):
                    nonlocal pending_document
                    pending_document = result
                    caption_source = {k: v for k, v in result.items() if k != 'content_b64'}
                    return await humanize(text, caption_source, cfg['humanize'], community=community, language=reply_language, is_owner=tier == 'owner', vertical_id=vertical_id)
                if result.get('result'):
                    # Confirmed live: skipping humanize() here (unlike
                    # every other branch in this function) let Analysis
                    # Agent's own ReAct-loop answer reach WhatsApp
                    # verbatim - grammatically fine but report-toned
                    # ("Based on the provided information, the user is
                    # a vegetarian...") rather than a natural reply, the
                    # one branch in this whole function that skipped
                    # the humanize step everything else already gets.
                    return await humanize(text, result, cfg['humanize'], community=community, language=reply_language, is_owner=tier == 'owner', vertical_id=vertical_id)
                return "Sorry, I'm not sure how to help with that yet."

            target_url = await route_to_agent(text, cfg['route_to_agent'])
            if target_url is None:
                reply = await _fallback_to_analysis()
            else:
                try:
                    result = await call_agent_with_text(target_url, augmented_text, token=token)
                except RuntimeError as e:
                    # Confirmed live, the actual bug this fixes: route_to_agent()
                    # picks a target agent by classifying against the FULL
                    # cross-agent skill pool, then call_agent_with_text() makes
                    # that agent classify the SAME text again from scratch,
                    # against only ITS OWN skills - two independent classify()
                    # calls that can disagree. When they do, the target agent's
                    # own skill_router rejects the text ("None of my skills
                    # match that request." / "Did you mean one of: ...?") and
                    # this used to be treated as a hard failure - the message
                    # was silently dropped even though Analysis Agent could
                    # have handled it, the same way it already handles
                    # anything route_to_agent() itself failed to classify.
                    # Narrowed to specifically this rejection shape (not any
                    # RuntimeError) so a genuine crash on the target agent
                    # still surfaces as a failure rather than being papered
                    # over by a fallback that might mask a real bug there.
                    error_text = str(e)
                    if 'None of my skills match' not in error_text and 'Did you mean one of' not in error_text:
                        raise
                    logger.warning(f'{target_url} rejected the routed text on its own reclassification, falling back to Analysis Agent: {error_text}')
                    reply = await _fallback_to_analysis()
                else:
                    if result.get('content_b64'):
                        # See this module's own docstring on the content_b64
                        # delivery convention. Humanize a caption from
                        # everything except the blob itself - passing the raw
                        # base64 into the humanize prompt would bloat it for no
                        # reason, and result.get('content_b64') is already the
                        # deliver-a-file signal, not something the caption needs
                        # to restate.
                        pending_document = result
                        caption_source = {k: v for k, v in result.items() if k != 'content_b64'}
                        reply = await humanize(text, caption_source, cfg['humanize'], community=community, language=reply_language, is_owner=tier == 'owner', vertical_id=vertical_id)
                    else:
                        reply = await humanize(text, result, cfg['humanize'], community=community, language=reply_language, is_owner=tier == 'owner', vertical_id=vertical_id)
        except Exception as e:
            # Confirmed live: this used to reply with the raw exception text
            # ("Did you mean one of: recall_contact_memory,
            # search_knowledge_base? Please clarify which one." - an
            # ambiguous-classify RuntimeError straight out of
            # mesh/lib/a2a_client.py's _extract_result) - internal,
            # technical, and not something a WhatsApp user should ever see.
            # Explicit instruction after that: a failure here means silence,
            # the same treatment already given to an unregistered stranger
            # above, not a best-effort apology message users have to parse.
            logger.error(f'Failed to handle message for {chat_id}: {e}')
            reply = None

    if reply is not None and just_auto_registered:
        # This identity was just silently auto-registered (a demo vertical,
        # or a real one opted into open_enrollment - see rules_engine.py's
        # own docstring) with no explicit "register me" from them, so this
        # is their very first reply ever. Prepended, not appended - a
        # privacy disclosure buried after the actual answer is easy to
        # skim past; the explicit-registration path's own REGISTERED_REPLY
        # already leads with the same wording. Only reply is None means a
        # genuine failure (see the except block just above) - never prepend
        # a disclosure to silence.
        reply = rules_engine.AUTO_REGISTERED_NOTICE + reply

    if reply is None:
        # Either the branch above failed outright, or nothing in this
        # module ever set a reply (should be unreachable given every branch
        # above sets one on success) - either way, no reply means no send.
        return {'chat_id': chat_id, 'reply': None, 'delivered': False}

    try:
        # Orchestrator's own dedicated tier, not the shared 'service' one -
        # delivering a reply (including a rejection to a stranger who has
        # no tier at all) is Orchestrator's own action, not something
        # authorized by whoever triggered this run. Confirmed live this
        # matters: 'service' lost mcp.whatsapp.send_message in the
        # 2026-08-30 lockdown, which silently broke every single reply
        # this sends - including to the owner's own self-chat - until
        # orchestrator_delivery was carved out specifically for this call.
        delivery_token = permissions.mint_token('orchestrator', 'orchestrator_delivery')
        if pending_image is not None:
            await call_tool(whatsapp_mcp_url, 'send_image', {
                'chat_id': chat_id,
                'filename': pending_image['filename'],
                'content_b64': pending_image['content_b64'],
                'mimetype': pending_image.get('mimetype') or 'image/png',
                'caption': reply,
            }, token=delivery_token)
        elif pending_document is not None:
            await call_tool(whatsapp_mcp_url, 'send_document', {
                'chat_id': chat_id,
                'filename': pending_document['filename'],
                'content_b64': pending_document['content_b64'],
                'mimetype': pending_document.get('mimetype') or 'application/octet-stream',
                'caption': reply,
            }, token=delivery_token)
        else:
            await call_tool(
                whatsapp_mcp_url, 'send_message', {'chat_id': chat_id, 'text': reply}, token=delivery_token,
            )
        delivered = True
    except Exception as e:
        # Confirmed live: an uncaught delivery failure here crashed the
        # whole handle_message task with an opaque TaskGroup error - even
        # though routing/forwarding/humanizing had already genuinely
        # succeeded. Delivery failing is a separate, distinct outcome, same
        # fix shape already applied once in the retired whatsapp_connector.
        #
        # Logged (unlike the "stay silent" reply=None path above) because
        # silence here is a UX choice, not a debugging one - confirmed live
        # this failure mode left zero trace anywhere (no error in this log,
        # no send_message call in whatsapp_mcp's own log) when it actually
        # happened, indistinguishable from routing having silently decided
        # not to reply at all.
        logger.error(f'Failed to deliver reply for {chat_id}: {describe_exception(e)}')
        delivered = False
        reply = f'{reply} (delivery failed: {e})'

    if should_remember:
        # Short-term: an in-process rolling window (mesh/lib/chat_cache.py),
        # separate from mem0's long-term semantic store below - never raises,
        # so no try/except needed around it. Same "regardless of `delivered`"
        # reasoning as the mem0 write just below applies here too.
        chat_cache.remember_turn(contact_name or chat_id, text, reply, vertical_id=vertical_id)

        # Long-term: best-effort, after delivery - see
        # mesh/memory/mem0_backend.py's own docstring for what actually
        # happens to this on the Memory Agent side. A failed memory write
        # must never affect a reply that's already been sent (or already
        # failed to send, for its own separate reasons) - remembered
        # regardless of `delivered`, since the exchange itself happened
        # either way.
        try:
            memory_url = router.get_agent_url('memory')
            if memory_url is not None:
                remember_token = permissions.mint_token('orchestrator', 'service')
                await call_agent(memory_url, 'remember_interaction', {
                    'contact_name': contact_name or chat_id,
                    'user_text': text,
                    'reply_text': reply,
                    'vertical_id': vertical_id,
                }, token=remember_token)
        except Exception as e:
            logger.warning(f'Failed to remember interaction for {chat_id}: {e}')

        # customer_record's own customer_section - separate from mem0 above
        # (a different store, a different purpose: a durable per-business
        # record, not general-purpose semantic recall) and only attempted
        # when a real vertical applies to this message. Never for the owner
        # - there's no "customer" to record when the owner is talking to
        # their own number, same is_owner reasoning persona_hook.py's own
        # docstring already documents for business_persona_context.
        if vertical_id and tier != 'owner':
            identity_key = db.resolve_identity_key(chat_id)
            await _observe_customer(text, reply or '', vertical_id, identity_key, cfg['extract_parameters'])

    return {'chat_id': chat_id, 'reply': reply, 'delivered': delivered}
