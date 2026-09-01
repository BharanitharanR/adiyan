"""
whatsapp - an MCP server exposing send_message as a callable tool, plus an
internal webhook listener that pushes each incoming WhatsApp message into
Orchestrator Agent via A2A. Same dual-role shape as cron_trigger: a normal
MCP tool on one side, an autonomous pusher on the other - receiving a push
notification and initiating an outbound A2A call isn't something MCP itself
has a primitive for, same reasoning documented in
mesh/mcp/cron_trigger/server.py.

Must run as a persistent process - a webhook listener can't be spawned fresh
per call, same argument as cron_trigger and workspace-mcp.

Replaces mesh/whatsapp_connector/, which combined webhook-receiving and
routing in one non-A2A bridge. Split correctly here: this component only
knows about WhatsApp itself (send + receive) - Orchestrator Agent (a real
A2A agent, mesh/orchestrator/) owns the actual routing/reply decision.

custom_route() (not a second server on a second port) adds the webhook onto
this same FastMCP app - confirmed available in the installed mcp package.

Run from the repo root as `python -m mesh.mcp.whatsapp.server`.
Requires: pip install "mcp[cli]" httpx starlette uvicorn
"""
import base64
import logging
from typing import Any, Dict, Optional

import uvicorn
from mcp.server.fastmcp import Context, FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from mesh.lib import permissions
from mesh.lib.a2a_client import call_agent
from mesh.lib.paths import mcp_home, mcp_state_db_path
from mesh.lib.tls import ensure_self_signed_cert
from mesh.lib.utilities.whatsapp.openwa_receiver import OpenWAAdapter
from mesh.lib.utilities.whatsapp.openwa_service import OpenWAService
from mesh.mcp.whatsapp import dedup

SERVER_NAME = 'whatsapp'
HOST = '127.0.0.1'
PORT = 8425

# Deliberately not /webhook/openwa - that's the legacy path
# (ui/control_panel_api.py). Different name so logs/greps never confuse the
# two systems, even though the legacy orchestrator is now stopped.
WEBHOOK_PATH = '/webhook/whatsapp'

OPENWA_URL = 'http://localhost:2785'
OPENWA_SESSION_NAME = 'adiyan'
ORCHESTRATOR_URL = 'http://127.0.0.1:8426'

logger = logging.getLogger(SERVER_NAME)

mcp = FastMCP(SERVER_NAME, host=HOST, port=PORT)
_adapter = OpenWAAdapter()
_openwa = OpenWAService(base_url=OPENWA_URL, api_key='', session_name=OPENWA_SESSION_NAME)
_dedup_conn = dedup.connect(mcp_state_db_path(SERVER_NAME))


@mcp.tool()
async def send_message(chat_id: str, text: str, ctx: Context) -> Dict[str, Any]:
    """Sends a WhatsApp message to chat_id. Raises on delivery failure -
    callers (Orchestrator Agent) decide what a failed send means for their
    own domain, same as every other tool call in this system."""
    permissions.enforce_mcp_permission(ctx, 'mcp.whatsapp.send_message')
    result = await _openwa.send_message(chat_id, text)
    return {'sent': True, 'result': result}


@mcp.tool()
async def send_document(
    chat_id: str, filename: str, content_b64: str, mimetype: str, ctx: Context, caption: Optional[str] = None,
) -> Dict[str, Any]:
    """Sends a document (base64-encoded) to chat_id, e.g. a knowledge-base
    file mesh/memory Agent is handing back (see
    mesh/orchestrator/skills/handle_message.py's content_b64 delivery
    branch). Same failure contract as send_message - raises, doesn't
    swallow, delivery failures."""
    permissions.enforce_mcp_permission(ctx, 'mcp.whatsapp.send_document')
    content = base64.b64decode(content_b64)
    result = await _openwa.send_document(chat_id, filename, content, mimetype=mimetype, caption=caption)
    return {'sent': True, 'result': result}


@mcp.tool()
async def send_image(
    chat_id: str, filename: str, content_b64: str, mimetype: str, ctx: Context, caption: Optional[str] = None,
) -> Dict[str, Any]:
    """Sends an image (base64-encoded) to chat_id, rendered as a real photo
    in the chat, not a file attachment - use this for an actual image
    (e.g. a logo), never send_document. Confirmed live: send_document with
    an image mimetype hangs until it times out, every time - OpenWA's own
    send-document and send-image are genuinely different message types,
    not two names for the same thing."""
    permissions.enforce_mcp_permission(ctx, 'mcp.whatsapp.send_image')
    content = base64.b64decode(content_b64)
    result = await _openwa.send_image(chat_id, filename, content, mimetype=mimetype, caption=caption)
    return {'sent': True, 'result': result}


@mcp.tool()
async def resolve_chat_id(phone: str, ctx: Context) -> Dict[str, Any]:
    """Resolves a phone number to its real WhatsApp chat id - not simply
    f'{phone}@c.us', since some contacts are addressed via WhatsApp's
    newer @lid scheme instead, with a lid number that bears no relation
    to the phone number at all. See OpenWAService.resolve_chat_id's own
    docstring for the full mechanism (including the already-a-JID
    short-circuit for a contact whose only known identifier is a lid).
    {'chat_id': None} if OpenWA has no match - never raises for "not
    found", since an unresolvable number is an expected outcome for a
    caller to check, not a server error."""
    permissions.enforce_mcp_permission(ctx, 'mcp.whatsapp.resolve_chat_id')
    chat_id = await _openwa.resolve_chat_id(phone)
    return {'chat_id': chat_id}


@mcp.tool()
async def send_voice(chat_id: str, content_b64: str, ctx: Context, mimetype: str = 'audio/ogg; codecs=opus') -> Dict[str, Any]:
    """Sends audio (base64-encoded) to chat_id as a real WhatsApp voice
    note (PTT - mic bubble + waveform), not a plain audio-file
    attachment - see OpenWAService.send_voice's own docstring for the
    Opus/OGG encoding requirement. Same failure contract as
    send_message - raises, doesn't swallow, delivery failures."""
    permissions.enforce_mcp_permission(ctx, 'mcp.whatsapp.send_voice')
    content = base64.b64decode(content_b64)
    result = await _openwa.send_voice(chat_id, content, mimetype=mimetype)
    return {'sent': True, 'result': result}


@mcp.tool()
async def get_own_phone(ctx: Context) -> Dict[str, Any]:
    """The linked account's own phone number - lets a caller (Orchestrator's
    rules engine) recognize the owner without Orchestrator ever touching
    WhatsApp's own API directly.

    Deliberately get_session_status()['phone'], not get_own_chat_id() -
    the latter also calls resolve_chat_id(), which hits OpenWA's
    contacts/check endpoint. That endpoint depends on the whatsapp-web.js
    Puppeteer/Chromium engine actually being responsive - confirmed live to
    hang indefinitely (ProtocolError: Runtime.callFunctionOn timed out) even
    while the session's own control-plane record correctly reports 'ready'.
    get_session_status() is a plain DB-backed read with no such dependency."""
    permissions.enforce_mcp_permission(ctx, 'mcp.whatsapp.get_own_phone')
    status = await _openwa.get_session_status()
    return {'phone': status.get('phone')}


async def _resolve_from_number(message: Dict[str, Any]) -> str:
    """The parsed from_number when it's trustworthy, or the live session's
    own phone when it isn't. In a genuine self-chat, message['from_me'] is
    unconditionally the account owner - confirmed live that WhatsApp
    reports the `from` JID inconsistently there (sometimes the real
    phone-form JID, sometimes the same `@lid` the chat itself is addressed
    by), which silently broke owner recognition in a self-chat even though
    there is structurally no other possible sender for that event.

    Gated on is_self_chat now, not from_me alone - confirmed live that
    from_me is true any time the owner's own device sends into ANY chat,
    not just the self-chat (e.g. the owner typing a follow-up message
    directly into a registered client's chat while testing). Trusting
    from_me unconditionally there collapsed a real client's identity to
    the owner's own number - their reading job got resolved to the
    owner's, and the reply was delivered back into the owner's own chat
    instead of the client's."""
    if message['from_me'] and message.get('is_self_chat'):
        status = await _openwa.get_session_status()
        return status.get('phone') or message['from_number']
    return message['from_number']


async def _resolve_outbound_media(message: Dict[str, Any]) -> Optional[str]:
    """The full base64 media data for an outbound (self-chat, fromMe=true)
    message whose webhook payload omitted it - download_media()'s
    archive-backed endpoint 404s unconditionally for an outbound message
    (confirmed live: OpenWA's media archive only ever archives INBOUND
    media - "the message was sent BY this account" is explicitly excluded
    per its own docs, regardless of CHAT_MEDIA_ARCHIVE_ENABLED - the exact
    thing services/kb_ingestion_poller.py's own comment already documented
    for the legacy pipeline).

    Re-fetching via get_messages() sidesteps this: that endpoint's own
    response carries the full media inline in metadata.media.data,
    unaffected by both the archive's inbound-only restriction and the
    webhook's own smaller WEBHOOK_MEDIA_INLINE_MAX_BYTES cutoff. None if
    the message can't be found in the recent-messages window (e.g. it's
    since scrolled past the fetch limit) - the caller treats that as "no
    media available," same as any other resolution failure."""
    messages = await _openwa.get_messages(message['lid'], limit=20)
    target_id = message['whatsapp_message_id']
    for raw in messages:
        if target_id in (raw.get('id'), raw.get('waMessageId')):
            media_meta = (raw.get('metadata') or {}).get('media') or {}
            return media_meta.get('data')
    return None


async def _resolve_media(message: Dict[str, Any]) -> Any:
    """None if this message carries no media. Otherwise the complete blob as
    {kind, mimetype, data (base64), filename} - mechanical WhatsApp I/O only
    (fetching the full blob when OpenWA didn't inline it), never an opinion
    on what the content means. kind is 'image' or 'document', straight from
    openwa_receiver.py's own parse of OpenWA's message type; filename is
    only ever populated for a document (WhatsApp never gives an image one).

    What an image means is still Orchestrator's own routing-layer decision
    (mesh/lib/vision.py's classify_image, used from
    mesh/orchestrator/skills/handle_message.py) - this component only knows
    WhatsApp send/receive mechanics, same as it only forwards raw text
    without interpreting it. A document's meaning is never ambiguous the
    way an image's is, so Orchestrator skips vision classification for it
    entirely - see handle_message.py's own document parameter."""
    media = message.get('media')
    if not media:
        return None

    content_b64 = media['data']
    if media['omitted']:
        # Blob was too large to inline (WEBHOOK_MEDIA_INLINE_MAX_BYTES) -
        # fetch it directly rather than losing the content.
        if message['from_me']:
            content_b64 = await _resolve_outbound_media(message)
        else:
            downloaded = await _openwa.download_media(message['lid'], message['whatsapp_message_id'])
            content_b64 = base64.b64encode(downloaded['content']).decode('ascii')

    if not content_b64:
        return None
    default_mimetype = 'image/jpeg' if media['kind'] == 'image' else 'application/octet-stream'
    return {
        'kind': media['kind'],
        'mimetype': media['mimetype'] or default_mimetype,
        'data': content_b64,
        'filename': media.get('filename'),
    }


@mcp.custom_route(WEBHOOK_PATH, methods=['POST'])
async def handle_webhook(request: Request) -> JSONResponse:
    payload = await request.json()
    message = await _adapter.webhook_to_message(payload)
    if not message:
        return JSONResponse({'status': 'ignored'})

    # Confirmed live: OpenWA redelivers a webhook when it doesn't get a
    # timely response, and Adiyan's LLM calls routinely run past whatever
    # window triggers that - with nothing to recognize a redelivery, each
    # one ran the full pipeline again as if it were new, producing 2-3
    # independent replies (and, without Scheduler's own separate embedding
    # dedup, would have created duplicate jobs) for a single message
    # actually sent once. Keyed on WhatsApp's own message id, not a content
    # hash - see dedup.py's docstring for why a content hash would
    # misfire on a genuinely repeated message (e.g. "Hi" sent twice).
    # Marked as seen before dispatch, not after, so two near-simultaneous
    # redeliveries can't both slip past the check.
    whatsapp_message_id = message['whatsapp_message_id']
    if dedup.is_duplicate(_dedup_conn, whatsapp_message_id):
        logger.info(f"Ignoring duplicate webhook delivery for message {whatsapp_message_id!r}")
        return JSONResponse({'status': 'duplicate_ignored'})
    dedup.mark_seen(_dedup_conn, whatsapp_message_id)

    resolved_media = await _resolve_media(message)
    image = resolved_media if resolved_media and resolved_media['kind'] == 'image' else None
    document = resolved_media if resolved_media and resolved_media['kind'] == 'document' else None

    try:
        await call_agent(ORCHESTRATOR_URL, 'handle_message', {
            'text': message['message_body'],
            'chat_id': message['lid'],
            'contact_name': message['contact_name'],
            'from_number': await _resolve_from_number(message),
            'image': image,
            'document': document,
            'is_self_chat': message['is_self_chat'],
        })
        return JSONResponse({'status': 'forwarded'})
    except Exception as e:
        # A failure to reach/complete on Orchestrator shouldn't crash the
        # webhook response - same "respond cleanly regardless" fix already
        # applied once in mesh/whatsapp_connector/server.py, carried over
        # here rather than reintroducing the same bug.
        logger.error(f'Failed to forward message to Orchestrator: {e}')
        return JSONResponse({'status': 'forward_failed', 'error': str(e)})


if __name__ == '__main__':
    # Not mcp.run_streamable_http_async() - that call has no TLS parameters
    # at all (confirmed: neither FastMCP.__init__ nor
    # run_streamable_http_async accept ssl_keyfile/ssl_certfile). Its own
    # source is nothing more than streamable_http_app() + uvicorn.Config +
    # uvicorn.Server().serve() - replicated here with SSL args added, same
    # behavior otherwise.
    cert_path, key_path = ensure_self_signed_cert(mcp_home(SERVER_NAME) / 'tls')
    uvicorn.run(
        mcp.streamable_http_app(),
        host=HOST,
        port=PORT,
        ssl_keyfile=str(key_path),
        ssl_certfile=str(cert_path),
    )
