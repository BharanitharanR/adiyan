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

from mesh.lib import permissions
from mesh.lib.errors import describe_exception
from mesh.lib.mcp_client import call_tool
from mesh.lib.skill_router import classify, extract
from mesh.orchestrator import db
from mesh.orchestrator.constants import WHATSAPP_MCP_URL

logger = logging.getLogger('RulesEngine')

REGISTER_PHRASE = 'register me'
UNREGISTER_PHRASE = 'unregister me'

# Case-insensitive substring match, deliberately not anchored to the start of
# the message - "can you check @Adiyan" and "@Adiyan what's next" both count.
ADIYAN_MENTION = '@adiyan'
_ADIYAN_MENTION_RE = re.compile(re.escape(ADIYAN_MENTION), re.IGNORECASE)


def strip_adiyan_mention(text: str) -> str:
    """Removes every @Adiyan mention before the text is handed to
    routing/classification, so the mention itself isn't just extra noise a
    skill classifier has to ignore. Safe to call unconditionally, including
    on a registered client's own message that never had a mention to begin
    with - a no-op in that case."""
    return _ADIYAN_MENTION_RE.sub('', text).strip()

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
) -> Tuple[Optional[str], Optional[str]]:
    """Returns (reply, tier). reply is set if the rules engine has already
    handled this message (registration, unregistration, or an add-named-
    contact admin command) - the caller should send that reply and stop,
    not route further. reply is None if the message should proceed to
    normal routing (the owner with an eligible, @Adiyan-mentioned message,
    or an already-registered contact's own message with no
    register/unregister command in it) - tier is then the permission tier
    (mesh/lib/permissions.py) to mint a token for before routing.

    is_self_chat matters only for deciding whether an owner-authored message
    (is_owner(from_number) is True) should trigger at all - see the owner
    branch below. Confirmed live why this has to be more than just
    "is_owner": treating every message you send, to any chat, as an Adiyan
    command meant an ordinary conversation with a real contact ("Hi" to
    Sripriya) got hijacked by Adiyan replying into that same conversation
    with a confused fallback message - "is_owner" alone can't tell an actual
    command from you just talking to someone.

    (None, None) - reply AND tier both unset - means either an unregistered
    stranger with no register/unregister command, or an owner-authored
    message that isn't eligible to trigger Adiyan at all (see below): stay
    completely silent, same as the legacy ValidatorAgent's behavior for the
    stranger case. Confirmed live why the stranger case has to be true
    silence, not even a canned rejection reply: an active reply to every
    unregistered sender, with no group exclusion upstream either, is what
    let a single WhatsApp group flood back a "not registered" reply into
    itself for every message any member sent. The caller must check for
    this exact (None, None) combination and stop before ever calling
    send_message - it's the only combination that means "send nothing," as
    opposed to (None, tier) meaning "proceed to
    routing," so don't restructure this without preserving that."""
    # Every clients-table read/write below goes through this, never the raw
    # chat_id - see db.resolve_identity_key()'s docstring: the same contact
    # can be addressed in different JID forms depending on which field you
    # read, so comparing raw chat_id values directly is unreliable.
    identity_key = db.resolve_identity_key(chat_id)

    if await is_owner(from_number):
        admin_reply = await _try_add_named_contact(conn, text, cfg)
        if admin_reply is not None:
            return admin_reply, 'owner'

        # An owner-authored message only ever triggers Adiyan in your own
        # self-chat or an already-registered client's chat - never in a
        # conversation with someone who isn't a client - AND only when
        # explicitly @-mentioned there too, so ordinary chatting (including
        # with a registered client) never fires Adiyan just because you sent
        # a message. See this function's docstring for the incident this
        # closes.
        eligible_chat = is_self_chat or db.is_whitelisted(conn, identity_key)
        if not eligible_chat or ADIYAN_MENTION not in text.lower():
            return None, None
        return None, 'owner'

    command = _detect_command(text)
    whitelisted = db.is_whitelisted(conn, identity_key)

    if command == 'register':
        db.add_client(conn, identity_key, contact_name)
        return REGISTERED_REPLY, permissions.default_client_tier()
    if command == 'unregister':
        if whitelisted:
            db.remove_client(conn, identity_key)
            return UNREGISTERED_REPLY, None
        else:
            return None, None
    if not whitelisted:
        # Silent, not a rejection reply - see check()'s docstring for why.
        return None, None

    tier = db.get_metadata(conn, identity_key).get('permission_type') or permissions.default_client_tier()
    return None, tier
