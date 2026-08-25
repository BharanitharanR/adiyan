"""
Outgoing-message watermark - appended to every message
OpenWAService.send_message sends, enforced at that single choke point so no
caller (mesh/ or the legacy pipeline) can send unwatermarked.

Mongo-backed via config_sdk, under config_sdk.CONTROL_AGENT_ID - this isn't
owned by any single agent (OpenWAService is a shared utility called from
Scheduler, Orchestrator, and the WhatsApp MCP alike), the same category
CONTROL_AGENT_ID already exists for (see config_sdk.py's own
_resolve_vertical(), which special-cases it to always resolve straight to
the platform layer - exactly the behavior wanted for a deployment-wide
setting like this one). Editable from the config dashboard the same way
any other constant is, with config_sdk's own short-TTL cache doing the
work the old mtime-cached watermark.json file used to do by hand.
"""
from mesh.lib import config_sdk

DEFAULT_TEXT = '[அடியேன் - at your service]'


async def apply(text: str) -> str:
    """Appends the current watermark to an outgoing message. An empty
    watermark_text constant is a deliberate off-switch, not an error -
    returns the message unchanged in that case."""
    watermark = await config_sdk.get_constant(
        config_sdk.CONTROL_AGENT_ID, 'watermark_text', DEFAULT_TEXT,
        description='Signature appended to every outgoing WhatsApp message. Leave empty to disable it entirely.',
    )
    if not watermark:
        return text
    return f'{text}\n\n{watermark}'


async def has_watermark(text: str) -> bool:
    """True if `text` carries the current watermark - the signal a
    message.sent webhook event uses to tell "Adiyan sent this itself" (via
    OpenWAService.send_message, which always calls apply()) apart from a
    message the owner composed by hand on their linked phone, which never
    passes through apply(). An empty/disabled watermark never matches -
    with the off-switch engaged there's no marker left to detect."""
    watermark = await config_sdk.get_constant(
        config_sdk.CONTROL_AGENT_ID, 'watermark_text', DEFAULT_TEXT,
        description='Signature appended to every outgoing WhatsApp message. Leave empty to disable it entirely.',
    )
    if not watermark:
        return False
    return watermark in text
