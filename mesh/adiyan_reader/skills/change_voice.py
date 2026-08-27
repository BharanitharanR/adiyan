"""
change_voice's real body - sets or clears one reading job's explicit voice
override. DataPart-only, reading_job_id + voice both real identifiers a
caller already has to hand, never something an extraction schema should
invent (same "no guessing a real key from prose" rule every other skill in
this module already follows).

voice='' clears the override, going back to following default_voice live
(see read_next_page.py's own resolution at read time, and
start_reading.py's own docstring on why '' means "no explicit choice" -
this is the same convention, applied after the fact instead of only at
creation).
"""
from typing import Any, Dict

from mesh.adiyan_reader import db
from mesh.adiyan_reader.constants import AGENT_ID
from mesh.adiyan_reader.tts import VOICES
from mesh.lib import config_sdk
from mesh.lib.paths import state_db_path


async def run(reading_job_id: str, voice: str) -> Dict[str, Any]:
    conn = db.connect(state_db_path(AGENT_ID))
    job = db.get_reading_job(conn, reading_job_id)
    if job is None:
        return {'changed': False, 'error': 'No reading job found with that id.'}

    if voice != '':
        available_voices = await config_sdk.get_constant(
            AGENT_ID, 'available_voices', list(VOICES),
            description='The Orpheus voice names this deployment allows readers to pick from.',
        )
        if voice not in available_voices:
            return {'changed': False, 'error': f'{voice!r} is not one of the available voices: {available_voices}'}

    db.set_reading_job_voice(conn, reading_job_id, voice)
    return {'changed': True, 'reading_job_id': reading_job_id, 'voice': voice or 'default'}
