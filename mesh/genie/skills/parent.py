"""
WhatsApp updates to the child's parents, sent through Adiyan's own WhatsApp
MCP server (the same path config_server's OTP login uses).

The tablet never chooses who gets a message. A parent's number only becomes
a destination after it is verified: parent_verify_start sends a 6-digit code
to that number, and parent_verify_confirm adds the number once the grown-up
types the code into the app. notify_parent then sends to verified numbers
only - so someone who got hold of the device key could, at worst, send the
parents a message, never anyone else. Rate limits cap both codes and
notifications.

State lives in this agent's own SQLite file (mesh/lib/paths.state_db_path).
"""
import secrets
import sqlite3
import time
from datetime import date
from typing import Any, Dict, List

from mesh.genie.constants import AGENT_ID
from mesh.lib import config_sdk, permissions
from mesh.lib.mcp_client import call_tool
from mesh.lib.paths import state_db_path

WHATSAPP_MCP_URL = 'https://127.0.0.1:8425/mcp'
TIER = 'genie_service'
CODE_TTL_SECONDS = 600
MAX_CODES_PER_HOUR = 3
MAX_CODE_TRIES = 5
MAX_NOTIFICATIONS_PER_DAY = 80
MAX_TEXT = 600


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(state_db_path(AGENT_ID))
    conn.execute('CREATE TABLE IF NOT EXISTS parents (phone TEXT PRIMARY KEY, chat_id TEXT NOT NULL, verified_at REAL NOT NULL)')
    conn.execute('CREATE TABLE IF NOT EXISTS codes (phone TEXT PRIMARY KEY, code TEXT NOT NULL, chat_id TEXT NOT NULL, '
                 'expires_at REAL NOT NULL, tries INTEGER NOT NULL DEFAULT 0)')
    conn.execute('CREATE TABLE IF NOT EXISTS code_log (sent_at REAL NOT NULL)')
    conn.execute('CREATE TABLE IF NOT EXISTS notify_log (day TEXT PRIMARY KEY, count INTEGER NOT NULL)')
    return conn


def _digits(phone: str) -> str:
    d = ''.join(ch for ch in (phone or '') if ch.isdigit())
    if not 10 <= len(d) <= 15:
        raise ValueError('Give the phone number with its country code, e.g. +91 98765 43210.')
    return d


def _masked(phone: str) -> str:
    return '+' + phone[:2] + ' ' + '•' * (len(phone) - 6) + phone[-4:]


async def _token() -> str:
    return permissions.mint_token(AGENT_ID, TIER)


async def _chat_id(phone: str) -> str:
    try:
        result = await call_tool(WHATSAPP_MCP_URL, 'resolve_chat_id', {'phone': phone}, token=await _token())
        if isinstance(result, dict) and result.get('chat_id'):
            return result['chat_id']
    except Exception:
        pass
    return f'{phone}@c.us'


async def _send(chat_id: str, text: str) -> None:
    await call_tool(WHATSAPP_MCP_URL, 'send_message', {'chat_id': chat_id, 'text': text}, token=await _token())


async def verify_start(phone: str) -> Dict[str, Any]:
    phone = _digits(phone)
    conn = _db()
    now = time.time()
    recent = conn.execute('SELECT COUNT(*) FROM code_log WHERE sent_at > ?', (now - 3600,)).fetchone()[0]
    if recent >= MAX_CODES_PER_HOUR:
        return {'sent': False, 'reason': 'Too many codes in the last hour. Try again later.'}
    code = f'{secrets.randbelow(1_000_000):06d}'
    chat_id = await _chat_id(phone)
    app_name = await config_sdk.get_constant(AGENT_ID, 'parent_app_name', 'Daily Practice',
                                             description='Name used at the start of WhatsApp updates to parents.')
    await _send(chat_id, f'{app_name}: your code to turn on WhatsApp updates is {code}. '
                         f'It expires in 10 minutes. If you did not ask for this, you can ignore it.')
    conn.execute('INSERT OR REPLACE INTO codes (phone, code, chat_id, expires_at, tries) VALUES (?, ?, ?, ?, 0)',
                  (phone, code, chat_id, now + CODE_TTL_SECONDS))
    conn.execute('INSERT INTO code_log (sent_at) VALUES (?)', (now,))
    conn.commit()
    return {'sent': True, 'to': _masked(phone), 'expiresInSeconds': CODE_TTL_SECONDS}


async def verify_confirm(phone: str, code: str) -> Dict[str, Any]:
    phone = _digits(phone)
    conn = _db()
    row = conn.execute('SELECT code, chat_id, expires_at, tries FROM codes WHERE phone = ?', (phone,)).fetchone()
    if row is None:
        return {'verified': False, 'reason': 'No code was sent to that number. Send a new code.'}
    expected, chat_id, expires_at, tries = row
    if time.time() > expires_at or tries >= MAX_CODE_TRIES:
        conn.execute('DELETE FROM codes WHERE phone = ?', (phone,))
        conn.commit()
        return {'verified': False, 'reason': 'That code has expired. Send a new code.'}
    if not secrets.compare_digest(str(code or '').strip(), expected):
        conn.execute('UPDATE codes SET tries = tries + 1 WHERE phone = ?', (phone,))
        conn.commit()
        return {'verified': False, 'reason': 'That code is not right. Check the WhatsApp message and try again.'}
    conn.execute('INSERT OR REPLACE INTO parents (phone, chat_id, verified_at) VALUES (?, ?, ?)', (phone, chat_id, time.time()))
    conn.execute('DELETE FROM codes WHERE phone = ?', (phone,))
    conn.commit()
    return {'verified': True, 'phone': _masked(phone)}


async def status() -> Dict[str, Any]:
    rows = _db().execute('SELECT phone FROM parents ORDER BY verified_at').fetchall()
    return {'parents': [_masked(r[0]) for r in rows]}


async def remove(phone: str) -> Dict[str, Any]:
    phone = _digits(phone)
    conn = _db()
    conn.execute('DELETE FROM parents WHERE phone = ?', (phone,))
    conn.commit()
    return {'removed': _masked(phone)}


async def notify(text: str, event: str = 'update') -> Dict[str, Any]:
    text = ' '.join((text or '').split())[:MAX_TEXT]
    if not text:
        raise ValueError('text was empty.')
    conn = _db()
    parents: List[tuple] = conn.execute('SELECT phone, chat_id FROM parents').fetchall()
    if not parents:
        return {'sent': 0, 'reason': 'No verified parent numbers yet.'}
    today = date.today().isoformat()
    row = conn.execute('SELECT count FROM notify_log WHERE day = ?', (today,)).fetchone()
    if row and row[0] >= MAX_NOTIFICATIONS_PER_DAY:
        return {'sent': 0, 'reason': 'Daily limit of WhatsApp updates reached.'}
    app_name = await config_sdk.get_constant(AGENT_ID, 'parent_app_name', 'Daily Practice',
                                             description='Name used at the start of WhatsApp updates to parents.')
    sent = 0
    for _phone, chat_id in parents:
        try:
            await _send(chat_id, f'\U0001F4DA {app_name} · {text}')
            sent += 1
        except Exception:
            continue
    conn.execute('INSERT INTO notify_log (day, count) VALUES (?, 1) ON CONFLICT(day) DO UPDATE SET count = count + 1', (today,))
    conn.commit()
    return {'sent': sent, 'event': event}
