"""
Shared authorization library - every agent and every MCP server checks a
caller's permission through this one place before running anything, the
same "defined once, wired everywhere" shape as skill_router.py and
vision.py.

A short-lived signed token is how identity travels. It's minted exactly
once per WhatsApp message, at the one place that actually knows who's
asking (Orchestrator's rules_engine, right after resolving owner/client/
stranger against the WhatsApp identity and - for a client - their own
clients.metadata.permission_type), then carried on every downstream call:
an A2A call's SendMessageRequest.metadata field, or an MCP tool call's
Authorization header. No other agent or MCP server needs its own copy of
the clients table, or its own owner-detection logic - each one just
verifies the signature and trusts the claim, the same trust boundary a
JWT is for anywhere else.

Verified against the actual installed SDKs before this was wired in, not
assumed: a2a_pb2.SendMessageRequest has a real `metadata` field (a
protobuf Struct) that survives to RequestContext.metadata server-side;
mcp.server.streamable_http.py hands the live Starlette Request through to
ServerMessageMetadata(request_context=request), which the low-level
server's RequestContext.request field carries all the way to a tool
function's ctx.request_context.request.headers.
"""
import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

import jwt

from mesh.lib.secrets_vault import get_secret

CONFIG_PATH = Path(__file__).parent / 'permissions_config.json'
ALGORITHM = 'HS256'

# Short-lived on purpose: minted fresh per inbound WhatsApp message, never
# stored or reused across messages - there's no legitimate reason for one
# to still be valid an hour later, so a leaked/logged token is only ever a
# narrow window of exposure, not a standing credential.
TOKEN_TTL_SECONDS = 3600


def _signing_key() -> str:
    key = get_secret('PERMISSIONS_JWT_SECRET')
    if not key:
        raise RuntimeError(
            'PERMISSIONS_JWT_SECRET is not set in the vault - permission tokens cannot be minted or verified.'
        )
    return key


def _load_config() -> Dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text())


def default_client_tier() -> str:
    """The tier a newly registered client gets when their
    clients.metadata.permission_type hasn't been set to anything specific
    yet - config-driven so it can change without touching db.py."""
    return _load_config()['default_client_tier']


def mint_token(subject: str, tier: str) -> str:
    """subject: chat_id for a real WhatsApp identity, or 'service' for an
    internal machine caller with no WhatsApp identity behind it. tier: the
    permission tier that decides what subject can do - 'owner', 'service',
    or a client's own permission_type (usually 'standard', but
    clients.metadata.permission_type can be set to any tier defined in
    permissions_config.json)."""
    now = int(time.time())
    claims = {'sub': subject, 'tier': tier, 'iat': now, 'exp': now + TOKEN_TTL_SECONDS}
    return jwt.encode(claims, _signing_key(), algorithm=ALGORITHM)


def verify_token(token: Optional[str]) -> Optional[Dict[str, Any]]:
    """None for a missing, malformed, expired, or badly-signed token - the
    caller treats that exactly like an unauthenticated stranger, never as
    an error to work around or log noisily about. A valid token's claims
    dict otherwise ({'sub', 'tier', 'iat', 'exp'})."""
    if not token:
        return None
    try:
        return jwt.decode(token, _signing_key(), algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None


def token_from_mcp_context(ctx: Any) -> Optional[str]:
    """Pulls the bearer token out of an @mcp.tool() function's injected
    Context (add a `ctx: Context` parameter to receive it - FastMCP
    supplies it automatically, no caller-side change needed).

    ctx.request_context.request is the real Starlette Request, all the way
    from mcp/server/streamable_http.py's ServerMessageMetadata(request_
    context=request) - confirmed by reading that dispatch path, not
    assumed, before this was relied on for anything security-relevant."""
    request = getattr(ctx.request_context, 'request', None)
    if request is None:
        return None
    header = request.headers.get('authorization', '')
    if not header.lower().startswith('bearer '):
        return None
    return header[len('bearer '):]


class PermissionDenied(Exception):
    """Raised by enforce_mcp_permission() - an @mcp.tool() function lets
    this propagate as-is; the MCP SDK turns any raised exception into a
    normal tool-error result, no special handling needed at the call site."""


def enforce_mcp_permission(ctx: Any, permission_key: str) -> None:
    """One-line guard for the top of an @mcp.tool() function. Raises
    PermissionDenied instead of returning a bool - every tool in this repo
    is a plain 'do the thing or raise', not a 'check first, then decide
    what to do' shape, so this matches that instead of making every tool
    re-implement its own if-not-allowed branch."""
    token = token_from_mcp_context(ctx)
    claims = verify_token(token)
    if not is_allowed(claims, permission_key):
        raise PermissionDenied(f'Not authorized for {permission_key}')


def is_allowed(claims: Optional[Dict[str, Any]], permission_key: str) -> bool:
    """claims=None (no or invalid token) is never allowed anything - the
    permission config has no tier for 'nobody', by design, not by
    omission. permission_key is '<agent_id>.<skill_id>' for an A2A skill
    (e.g. 'scheduler.schedule_job') or 'mcp.<server_name>.<tool_name>' for
    an MCP tool (e.g. 'mcp.whatsapp.send_message') - one flat namespace,
    checked the same way regardless of which kind of call it is."""
    if not claims:
        return False
    tier = claims.get('tier')
    config = _load_config()
    rule = config['tiers'].get(tier)
    if not rule:
        return False
    allowed = rule.get('allow', [])
    return '*' in allowed or permission_key in allowed
