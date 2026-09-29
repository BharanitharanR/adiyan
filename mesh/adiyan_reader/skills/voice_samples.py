"""
voice_samples's real body - lets a customer hear a short sample of each
available narrator voice before picking one, rather than choosing blind from
a list of names. Sends one labeled voice note per voice (via
tts.get_or_create_voice_sample() - cached after the first request, so this
never regenerates a voice that's already been sampled before), then invites
a plain-text reply, which change_voice.py's own phone_number-based
resolution now handles.

DataPart-only, phone_number-only - same contract as read_now.py/
stop_reading.py: nothing here is guessed from free text.
"""
from typing import Any, Dict

from mesh.adiyan_reader import tts
from mesh.adiyan_reader.constants import AGENT_ID, OLLAMA_URL
from mesh.lib import config_sdk
from mesh.lib.agent_sdk import AdiyanAgent

_agent = AdiyanAgent(AGENT_ID)


async def run(phone_number: str) -> Dict[str, Any]:
    chat_id = await _agent.resolve_chat_id(phone_number)
    if chat_id is None:
        return {'status': 'failed', 'result_summary': f"Could not resolve WhatsApp chat for {phone_number}."}

    available_voices = await config_sdk.get_constant(
        AGENT_ID, 'available_voices', list(tts.VOICES),
        description='The Orpheus voice names this deployment allows readers to pick from.',
    )
    tts_cfg = await config_sdk.get_stage_config(
        AGENT_ID, 'synthesize_speech', {
            'model': 'legraphista/Orpheus:3b-ft-q8', 'base_url': OLLAMA_URL,
            'temperature': 0.6, 'top_p': 0.9, 'repetition_penalty': 1.3,
            'engine': 'orpheus', 'voicebox_profile_id': None,
        },
    )

    sent = []
    for voice in available_voices:
        try:
            audio = await tts.get_or_create_voice_sample(voice, tts_cfg)
        except Exception:
            # One voice failing to generate (a transient Ollama hiccup)
            # shouldn't stop the rest of the menu from going out - a
            # customer with 7 of 8 samples can still make a real choice.
            continue
        await _agent.send_message_to(chat_id, f'🎙️ {voice.capitalize()}')
        await _agent.send_voice_to(chat_id, audio)
        sent.append(voice)

    if not sent:
        return {'status': 'failed', 'result_summary': "Couldn't generate any voice samples right now - try again in a moment."}
    return {
        'status': 'sent', 'voices': sent,
        'result_summary': f"Sent samples of {', '.join(v.capitalize() for v in sent)} - just tell me which one you'd like!",
    }
