"""
WhatsApp rules engine - the gate in front of Orchestrator's routing.
Two jobs: registration/unregistration of numbers, and (once past the gate)
letting the message proceed to the existing routing logic in router.py.

Fixed trigger phrases, not LLM-classified - mirrors the legacy
agents/parser_agent.py's own reasoning exactly: an opt-in/opt-out gesture
is security-relevant, and a fixed phrase can't be hallucinated into firing
the way a model's classification could.

The owner is never subject to this gate - resolved via WhatsApp MCP's own
get_own_chat_id tool (Orchestrator still doesn't know WhatsApp's API
directly, only that this tool exists), not stored in the clients table at
all.
"""
import logging
import re
import sqlite3
from typing import Any, Dict, Optional, Tuple

from a2a.types import AgentSkill
from pydantic import BaseModel, Field

from mesh.lib import config_sdk, permissions
from mesh.lib.errors import describe_exception
from mesh.lib.mcp_client import call_tool
from mesh.lib.skill_router import classify, extract
from mesh.orchestrator import db
from mesh.orchestrator.constants import AGENT_ID, WHATSAPP_MCP_URL

logger = logging.getLogger('RulesEngine')

REGISTER_PHRASE = 'register me'
UNREGISTER_PHRASE = 'unregister me'

# The thumb rule (2026-09-10): Adiyan responds to a message ONLY if that
# message contains the summon phrase - every sender (owner and client
# alike), every chat. Adiyan's own replies never contain it, so an echo of
# a reply can never re-trigger Adiyan; and ordinary conversation in a
# client's chat never wakes it either. register/unregister are the only
# exceptions (their own opt-in gesture, a new client wouldn't know to
# summon first).
#
# Case-insensitive substring match, deliberately not anchored to the start
# of the message - "can you check @Adiyan" and "@Adiyan what's next" both
# count. Dashboard-editable via config_sdk (see get_summon_phrase); the
# constant here is only the default / the value used if Mongo is
# unreachable.
DEFAULT_SUMMON_PHRASE = '@adiyan'
# Back-compat alias - existing call sites / tests referencing the old name.
ADIYAN_MENTION = DEFAULT_SUMMON_PHRASE


async def get_summon_phrase() -> str:
    """The PLATFORM layer's own phrase - '@adiyan' unless the platform
    default itself was edited on the dashboard. Explicit vertical_id=
    PLATFORM_VERTICAL, not the old implicit "whatever's deployment-wide
    active" resolution - see _resolve_summoned_vertical()'s own docstring
    for why '@adiyan' now always means platform defaults specifically, even
    while another vertical's own phrase is also live. A blank configured
    value falls back to the default rather than meaning "respond to
    everything" - an empty summon phrase would re-open the exact
    ambient-reply / echo loop this gate exists to close."""
    phrase = await config_sdk.get_constant(
        AGENT_ID, 'summon_phrase', DEFAULT_SUMMON_PHRASE, vertical_id=config_sdk.PLATFORM_VERTICAL,
        description=(
            'Text a message must contain (anywhere, case-insensitive) for Adiyan to respond '
            'at all - applies to every sender and every chat. "register me" / "unregister me" '
            'still work without it. Blank falls back to the default.'
        ),
    )
    return (phrase or DEFAULT_SUMMON_PHRASE).strip().lower()


async def _resolve_summoned_vertical(text: str) -> Tuple[bool, Optional[str]]:
    """(summoned, vertical_id_or_None) - checks every real vertical's own
    summon_phrase FIRST (each an explicit, independent lookup, never the
    old implicit "whatever's deployment-wide active" fallback), then the
    platform's own '@adiyan'. Returns the first phrase actually found in
    the text; a vertical whose phrase isn't present doesn't apply, no
    matter how many other verticals exist.

    This is what lets N business verticals coexist on one deployment:
    "which vertical applies" is now a property of THIS message (which
    phrase it contains), not a single global toggle every agent asks
    config_sdk for. '@adiyan' always means platform defaults specifically
    now, even while a vertical's own phrase is also live - see
    get_summon_phrase()'s own docstring. If two verticals were ever
    misconfigured with the same phrase, the first one found wins silently;
    apply_vertical_spec.py is responsible for refusing that collision
    before it can ever be written.

    vertical_enabled (a constant, default True) is the deactivate_vertical/
    activate_vertical toggle under this model - a disabled vertical's
    AgentConfig documents (and its own summon_phrase) are left completely
    intact, just skipped here, so re-activating it later never needs the
    phrase re-entered or the spec re-uploaded.

    (False, None) means no phrase matched at all - the caller's existing
    thumb rule (no summon phrase, no response) applies unchanged."""
    text_lower = text.lower()
    for vertical_id in await config_sdk.list_vertical_ids(AGENT_ID):
        # get_vertical_own_constant, deliberately not get_constant - a
        # vertical that never set its own summon_phrase must be skipped
        # entirely here, not treated as if it explicitly matched the
        # platform default. Confirmed live this session as a real bug: a
        # leftover test vertical with no phrase of its own was silently
        # shadowing '@adiyan' the moment it was listed first.
        phrase = await config_sdk.get_vertical_own_constant(AGENT_ID, vertical_id, 'summon_phrase')
        if not isinstance(phrase, str) or not phrase.strip():
            continue
        enabled = await config_sdk.get_constant(AGENT_ID, 'vertical_enabled', True, vertical_id=vertical_id)
        if not enabled:
            continue
        if phrase.strip().lower() in text_lower:
            return True, vertical_id
    platform_phrase = await get_summon_phrase()
    if platform_phrase in text_lower:
        return True, None
    return False, None


# Same prefix mesh/mcp/whatsapp/verticals_demo.py's own _build_spec() gives every
# vertical it creates - never a real production vertical_id, which is chosen by
# the business owner via apply_vertical_spec.py, not generated. Checked here so
# the 30-minute no-phrase-needed session (see db.record_summon's own docstring)
# only ever applies to a live-portal-demo visitor's own conversation, not a real
# customer of an actual installed business - a deliberate scope decision, not a
# technical limitation of the session mechanism itself.
_DEMO_VERTICAL_PREFIX = 'demo-'


def _is_demo_vertical(vertical_id: Optional[str]) -> bool:
    return bool(vertical_id) and vertical_id.startswith(_DEMO_VERTICAL_PREFIX)


async def _fallback_to_active_session(
    conn: db.Collection, identity_key: str,
) -> Tuple[bool, Optional[str]]:
    """Only called once _resolve_summoned_vertical() has already found no
    phrase in THIS message - the same (False, None) shape that function
    returns, upgraded to (True, vertical_id) if this identity summoned a
    still-live demo vertical within the last db.SESSION_WINDOW_SECONDS.
    Re-checks vertical_enabled here rather than trusting the session blindly -
    a demo vertical's own TTL (verticals_demo.py's cron-scheduled expiry) can
    fire in the middle of an otherwise-still-valid 30-minute window, and a
    deactivated vertical must stay unreachable regardless of what the session
    says."""
    vertical_id = db.get_active_session_vertical(conn, identity_key)
    if not _is_demo_vertical(vertical_id):
        return False, None
    enabled = await config_sdk.get_constant(AGENT_ID, 'vertical_enabled', True, vertical_id=vertical_id)
    if not enabled:
        return False, None
    return True, vertical_id


def strip_summon_phrase(text: str, phrase: str = DEFAULT_SUMMON_PHRASE) -> str:
    """Removes every occurrence of the summon phrase before the text is
    handed to routing/classification, so the summon itself isn't just
    extra noise a skill classifier has to ignore. Safe to call
    unconditionally - a no-op for text that never had it."""
    return re.compile(re.escape(phrase), re.IGNORECASE).sub('', text).strip()


# Back-compat alias for the pre-2026-09-10 name.
def strip_adiyan_mention(text: str) -> str:
    return strip_summon_phrase(text, DEFAULT_SUMMON_PHRASE)

REGISTERED_REPLY = "You're registered! Ask away."
UNREGISTERED_REPLY = 'You\'ve been unregistered. Send "register me" any time to come back.'

# Owner-only, NL-classified (unlike register/unregister above) - there's no
# fixed phrase for "add a client on someone else's behalf" the way there is
# for self-registration, and it's gated on is_owner() being true before this
# is even attempted, so a hallucinated match can only ever affect the
# owner's own messages, never a stranger's.
ADD_CONTACT_SKILL = AgentSkill(
    id='add_named_contact',
    name='Add Named Contact',
    description='The owner registering a new client by name and phone number, so that person can message Adiyan directly.',
    tags=['admin', 'registration'],
    examples=[
        'Add Priya as a client, her number is 9198765432',
        'Register 919876543210 as Karthik',
        'New client: Ramesh, +91 90802 34567',
    ],
    input_modes=['text/plain'],
    output_modes=['application/json'],
)


class AddContactParams(BaseModel):
    name: str = Field(description="The contact's name, as given by the owner.")
    phone_number: str = Field(
        description="The contact's phone number exactly as given (digits, spaces, "
        "country code, '+' - whatever form the owner used). Do not invent or reformat it."
    )


def _detect_command(text: str) -> Optional[str]:
    """Word-boundary matched, unregister checked first - same reasoning as
    the legacy parser_agent.py: 'register me' is a substring of
    'unregister me', so checking register first would misfire on every
    unregister request too."""
    msg_lower = text.lower()
    if re.search(r'\b' + re.escape(UNREGISTER_PHRASE) + r'\b', msg_lower):
        return 'unregister'
    if re.search(r'\b' + re.escape(REGISTER_PHRASE) + r'\b', msg_lower):
        return 'register'
    return None


def _digits_only(phone: Optional[str]) -> str:
    return ''.join(ch for ch in (phone or '') if ch.isdigit())


async def is_owner(from_number: Optional[str]) -> bool:
    """Compares plain phone numbers (both digit-normalized), not chat_id -
    deliberately avoids the WhatsApp-engine-dependent chat_id resolution
    path. See get_own_phone()'s docstring in mesh/mcp/whatsapp/server.py for
    why: chat_id resolution hangs whenever the underlying browser engine is
    unresponsive, even while the session otherwise reports 'ready'."""
    if not from_number:
        return False
    # A service token, not the caller's - this check is what determines
    # whether there even is a resolved caller tier yet, so it can't use
    # one that doesn't exist until after this returns.
    token = permissions.mint_token('orchestrator', 'service')
    result = await call_tool(WHATSAPP_MCP_URL, 'get_own_phone', {}, token=token)
    own_phone = result.get('phone')
    return bool(own_phone) and _digits_only(own_phone) == _digits_only(from_number)


async def _try_add_named_contact(conn: sqlite3.Connection, text: str, cfg: Dict[str, Any]) -> Optional[str]:
    """None if this owner message isn't an add-contact command (the normal
    case - most owner messages are just normal requests, not admin
    commands) - falls through to routing exactly like any other owner
    message. A match adds the client and returns a confirmation."""
    # Real bug, confirmed live: this ran unconditionally for EVERY owner
    # message with no try/except at all, unlike every other classify()
    # call site in this codebase (_resolve_book_reading_request/
    # _resolve_read_now_request in handle_message.py both degrade
    # gracefully). With local Ollama unreachable, classify()'s own
    # ConnectionError propagated all the way up through check() and
    # crashed the whole run() function before it ever reached routing,
    # the communitySearch check, or anything else - meaning an owner
    # couldn't get ANY reply, not even a peer-offloaded one, while Ollama
    # was down, regardless of what they typed. Same degrade-on-failure
    # treatment as every other classify() site: not an add-contact
    # command, fall through to normal routing.
    try:
        choice = await classify(text, [ADD_CONTACT_SKILL], cfg)
    except Exception as e:
        logger.error(f'Add-contact intent classification failed: {describe_exception(e)}')
        return None
    if choice.skill_id != ADD_CONTACT_SKILL.id:
        return None

    try:
        params = await extract(text, ADD_CONTACT_SKILL.id, AddContactParams, cfg)
    except Exception as e:
        logger.error(f'Add-contact parameter extraction failed: {describe_exception(e)}')
        return None
    phone_digits = _digits_only(params.phone_number)
    if not phone_digits:
        return f"Couldn't find a valid phone number for {params.name} in that - try again with the digits included."

    # <digits>@c.us is OpenWA's own neutral phone-addressed JID (see
    # penwa/src/engine/identity/wa-id.ts) - constructed directly rather than
    # resolved via OpenWA's contacts/check endpoint, which depends on the
    # Puppeteer/Chromium engine actually being responsive (confirmed live to
    # hang indefinitely - see get_own_phone()'s docstring for the same issue).
    chat_id = f'{phone_digits}@c.us'
    db.add_client(conn, db.resolve_identity_key(chat_id), params.name)
    return f'Added {params.name} ({phone_digits}) as a registered client.'


async def check(
    conn: sqlite3.Connection,
    chat_id: str,
    contact_name: Optional[str],
    text: str,
    from_number: Optional[str],
    cfg: Dict[str, Any],
    is_self_chat: bool = False,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Returns (reply, tier, vertical_id). reply is set if the rules engine
    has already handled this message (registration, unregistration, or an
    add-named-contact admin command) - the caller should send that reply and
    stop, not route further. reply is None if the message should proceed to
    normal routing - which now requires a summon phrase to be present, for
    BOTH the owner (in their self-chat or a client's chat) AND a registered
    client. A registered client's message with no register/unregister
    command and no summon phrase returns (None, None, None): silence,
    UNLESS this identity summoned a still-enabled demo vertical
    (mesh/mcp/whatsapp/verticals_demo.py's 'demo-' prefix) within the last
    db.SESSION_WINDOW_SECONDS (see _fallback_to_active_session()) - a live-
    portal-demo visitor gets a real back-and-forth without repeating the
    phrase every message, for exactly that fixed window from their last
    explicit phrase, never longer. Real production verticals are
    deliberately excluded from this fallback; they keep the strict every-
    message-needs-the-phrase behavior described above. tier is the
    permission tier (mesh/lib/permissions.py) to mint a token for before
    routing.

    vertical_id is which business vertical's own phrase matched
    (_resolve_summoned_vertical()), or None for the platform default
    '@adiyan' / no match at all - the caller threads this into every
    mint_token() call for this message, which is what makes N verticals
    able to coexist on one deployment (see permissions.mint_token()'s own
    docstring): which vertical applies is resolved once, here, from this
    message's own text, not asked ambiently downstream by every agent.

    is_self_chat matters only for deciding whether an owner-authored message
    (is_owner(from_number) is True) should trigger at all - see the owner
    branch below. Confirmed live why this has to be more than just
    "is_owner": treating every message you send, to any chat, as an Adiyan
    command meant an ordinary conversation with a real contact ("Hi" to
    Sripriya) got hijacked by Adiyan replying into that same conversation
    with a confused fallback message - "is_owner" alone can't tell an actual
    command from you just talking to someone.

    (None, None, None) - reply AND tier both unset - means either an
    unregistered stranger with no register/unregister command, or an
    owner-authored message that isn't eligible to trigger Adiyan at all (see
    below): stay completely silent, same as the legacy ValidatorAgent's
    behavior for the stranger case. Confirmed live why the stranger case has
    to be true silence, not even a canned rejection reply: an active reply
    to every unregistered sender, with no group exclusion upstream either,
    is what let a single WhatsApp group flood back a "not registered" reply
    into itself for every message any member sent. The caller must check
    for reply is None and tier is None and stop before ever calling
    send_message - it's the only combination that means "send nothing," as
    opposed to (None, tier, ...) meaning "proceed to routing," so don't
    restructure this without preserving that."""
    # Every clients-table read/write below goes through this, never the raw
    # chat_id - see db.resolve_identity_key()'s docstring: the same contact
    # can be addressed in different JID forms depending on which field you
    # read, so comparing raw chat_id values directly is unreliable.
    identity_key = db.resolve_identity_key(chat_id)

    # The thumb rule: no summon phrase in the message, no response - for
    # anyone, in any chat (see this module's own header comment and the
    # 2026-09-10 runaway-loop incident). Resolved once here (against every
    # live vertical's own phrase, not just the platform default - see
    # _resolve_summoned_vertical()'s own docstring); register/unregister
    # below are the only things allowed past without it.
    summoned, vertical_id = await _resolve_summoned_vertical(text)

    if await is_owner(from_number):
        admin_reply = await _try_add_named_contact(conn, text, cfg)
        if admin_reply is not None:
            return admin_reply, 'owner', vertical_id

        # An owner-authored message only ever triggers Adiyan in your own
        # self-chat or an already-registered client's chat - never in a
        # conversation with someone who isn't a client - AND only when it
        # carries the summon phrase, so ordinary chatting (including with a
        # registered client) never fires Adiyan just because you sent a
        # message.
        eligible_chat = is_self_chat or db.is_whitelisted(conn, identity_key)
        if not eligible_chat:
            return None, None, None
        if not summoned:
            summoned, vertical_id = await _fallback_to_active_session(conn, identity_key)
            if not summoned:
                return None, None, None
        elif _is_demo_vertical(vertical_id):
            db.record_summon(conn, identity_key, vertical_id)
        return None, 'owner', vertical_id

    command = _detect_command(text)
    whitelisted = db.is_whitelisted(conn, identity_key)

    if command == 'register':
        db.add_client(conn, identity_key, contact_name)
        return REGISTERED_REPLY, permissions.default_client_tier(), None
    if command == 'unregister':
        if whitelisted:
            db.remove_client(conn, identity_key)
            return UNREGISTERED_REPLY, None, None
        else:
            return None, None, None
    if not whitelisted:
        # Silent, not a rejection reply - see check()'s docstring for why.
        return None, None, None

    # Registered client, real message: same thumb rule as the owner branch.
    # Without this, every message a client sent (and every watermark-less
    # echo of Adiyan's own reply) re-entered routing and could be answered -
    # the exact gap behind the 2026-09-10 loop in a client chat.
    if not summoned:
        summoned, vertical_id = await _fallback_to_active_session(conn, identity_key)
        if not summoned:
            return None, None, None
    elif _is_demo_vertical(vertical_id):
        db.record_summon(conn, identity_key, vertical_id)

    tier = db.get_metadata(conn, identity_key).get('permission_type') or permissions.default_client_tier()
    return None, tier, vertical_id
