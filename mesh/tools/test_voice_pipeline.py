#!/usr/bin/env python3
"""
Fetches your most recent WhatsApp voice note(s) and runs them through the
real transcription/routing pipeline directly - no need to hand-write a
throwaway script each time to check "did my last voice note work."

Two modes:
  --transcribe-only (default): just shows what Whisper produces, fast,
    safe to run repeatedly - never sends a reply, never touches routing.
  --full: also runs the transcribed text through the actual orchestrator
    pipeline (mesh/orchestrator/skills/handle_message.py's run()) exactly
    as a real incoming webhook would - this DOES send a real reply back
    to the chat if one is produced. Only use this when you actually want
    Adiyan to respond, same as sending the message for real would do.

Needs OpenWA and Ollama up (mesh/start_all.sh is enough) - MongoDB too if
using --full, since the real pipeline reads config/client state from it.

Run from the repo root:
    python3 -m mesh.tools.test_voice_pipeline
    python3 -m mesh.tools.test_voice_pipeline --limit 5
    python3 -m mesh.tools.test_voice_pipeline --chat-id 262366784700496@lid
    python3 -m mesh.tools.test_voice_pipeline --full
"""
import argparse
import asyncio
import base64

from mesh.lib.audio_transcribe import transcribe_audio
from mesh.lib.utilities.whatsapp.openwa_service import OpenWAService

OPENWA_URL = 'http://localhost:2785'
OPENWA_SESSION_NAME = 'adiyan'

AUDIO_KINDS = {'voice', 'audio', 'ptt'}


def _is_audio_message(m: dict) -> bool:
    if m.get('type') in AUDIO_KINDS:
        return True
    media = (m.get('metadata') or {}).get('media') or {}
    return (media.get('mimetype') or '').startswith('audio/')


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--chat-id', default=None, help="Chat to scan (default: your own self-chat, auto-resolved)")
    parser.add_argument('--limit', type=int, default=1, help="How many of the most recent audio messages to check (default: 1)")
    parser.add_argument('--scan', type=int, default=15, help="How many recent messages (of any type) to scan for audio ones (default: 15)")
    parser.add_argument('--full', action='store_true', help="Also run the transcribed text through the real orchestrator pipeline (sends a real reply)")
    parser.add_argument('--contact-name', default='Bharani', help="contact_name to pass through when using --full")
    parser.add_argument('--from-number', default=None, help="from_number to pass through when using --full (default: the linked session's own phone)")
    args = parser.parse_args()

    svc = OpenWAService(base_url=OPENWA_URL, api_key='', session_name=OPENWA_SESSION_NAME)

    chat_id = args.chat_id
    if chat_id is None:
        chat_id = await svc.get_own_chat_id()
        if chat_id is None:
            print('Could not resolve your own self-chat (session not linked yet?) - pass --chat-id explicitly.')
            return
        print(f'Using your own self-chat: {chat_id}')

    from_number = args.from_number
    if args.full and from_number is None:
        status = await svc.get_session_status()
        from_number = status.get('phone')

    messages = await svc.get_messages(chat_id, limit=args.scan)
    audio_messages = [m for m in messages if _is_audio_message(m)]
    if not audio_messages:
        print(f'No audio messages found in the most recent {args.scan} messages of {chat_id}.')
        return

    for i, m in enumerate(audio_messages[: args.limit]):
        media = m['metadata']['media']
        audio_bytes = base64.b64decode(media['data'])
        print(f"\n=== [{i}] {m.get('type')} | {m['timestamp']} | {len(audio_bytes)} bytes ===")

        text = await transcribe_audio(audio_bytes)
        print(f'  transcribed: {text!r}')

        if not args.full:
            continue
        if not text:
            print('  --full skipped: nothing to route (transcription failed/empty)')
            continue

        from mesh.orchestrator.skills import handle_message
        audio_payload = {
            'kind': m.get('type') or 'ptt', 'mimetype': media.get('mimetype'),
            'data': media['data'], 'omitted': False, 'filename': None,
        }
        result = await handle_message.run(
            text='', chat_id=chat_id, contact_name=args.contact_name, from_number=from_number,
            audio=audio_payload, is_self_chat=(chat_id == await svc.get_own_chat_id()),
        )
        print(f'  pipeline result: {result}')


if __name__ == '__main__':
    asyncio.run(main())
