"""
Outgoing-message watermark - appended to every message
OpenWAService.send_message sends, enforced at that single choke point so no
caller (mesh/ or the legacy pipeline) can send unwatermarked. File-backed
and mtime-cached, same pattern as agents/parser_agent.py's
trigger_words.json - configurable today by editing the file directly, and
the same file a future dashboard setting reads/writes without any format
change.
"""
import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger('Watermark')

DATA_DIR = Path.home() / '.Adiyan'
DATA_DIR.mkdir(exist_ok=True)
WATERMARK_FILE = DATA_DIR / 'watermark.json'

DEFAULT_TEXT = '[அடியேன் - at your service]'

_cached_text = DEFAULT_TEXT
_cached_mtime: Optional[float] = None


def _load() -> str:
    global _cached_text, _cached_mtime

    if not WATERMARK_FILE.exists():
        with open(WATERMARK_FILE, 'w', encoding='utf-8') as f:
            json.dump({'text': DEFAULT_TEXT}, f, indent=2, ensure_ascii=False)
        _cached_text = DEFAULT_TEXT
        _cached_mtime = WATERMARK_FILE.stat().st_mtime
        return _cached_text

    try:
        mtime = WATERMARK_FILE.stat().st_mtime
        if mtime == _cached_mtime:
            return _cached_text  # unchanged since last read

        with open(WATERMARK_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        _cached_text = str(data.get('text', DEFAULT_TEXT))
        _cached_mtime = mtime
    except Exception as e:
        logger.warning(f"Failed to load watermark.json, keeping previous watermark: {e}")

    return _cached_text


def apply(text: str) -> str:
    """Appends the current watermark to an outgoing message. An empty
    'text' value in watermark.json is a deliberate off-switch, not an
    error - returns the message unchanged in that case."""
    watermark = _load()
    if not watermark:
        return text
    return f'{text}\n\n{watermark}'


def has_watermark(text: str) -> bool:
    """True if `text` carries the current watermark - the signal a
    message.sent webhook event uses to tell "Adiyan sent this itself" (via
    OpenWAService.send_message, which always calls apply()) apart from a
    message the owner composed by hand on their linked phone, which never
    passes through apply(). An empty/disabled watermark never matches -
    with the off-switch engaged there's no marker left to detect."""
    watermark = _load()
    if not watermark:
        return False
    return watermark in text
