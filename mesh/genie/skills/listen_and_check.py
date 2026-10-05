"""
listen_and_check's real body: turn the child's voice into text, then check how
well he did. Reuses AdiyanReader's own transcribe-back-and-compare pipeline
(mesh/adiyan_reader/eval_quality.py) rather than a second copy of it:
Voicebox's Whisper endpoint for speech-to-text, and its semantic match score.

Two modes:

- reading: he read a passage aloud. On top of AdiyanReader's meaning score
  (did the sense of the passage come through), a word-by-word alignment of
  the transcript against the passage gives what a teacher's running record
  counts: words read correctly, skipped, changed, accuracy, and words correct
  per minute. The alignment is plain text matching (difflib), so the same
  recording always gives the same numbers.
- explanation: he said how he solved a problem. The transcript is checked
  against the problem-solving steps (understand, plan / choose the operation,
  number sentence, solve, check), each marked did / partly / missing with a
  quote from what he said as evidence.

Caveat worth keeping in mind: Whisper leans towards well-formed English, so
it can quietly "fix" a misread word. Reading accuracy from this is therefore
an upper bound; the recording itself is the ground truth.
"""
import asyncio
import base64
import re
import subprocess
import tempfile
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from mesh.adiyan_reader.eval_quality import _DEFAULT_STT_MODEL, _score_match, _transcribe
from mesh.adiyan_reader.tts import VOICEBOX_URL
from mesh.genie.constants import AGENT_ID
from mesh.lib import config_sdk
from mesh.lib.agent_sdk import AdiyanAgent
from mesh.lib.config import load_runtime_config

AGENT_CODE_DIR = Path(__file__).parent.parent
_agent = AdiyanAgent(AGENT_ID)
MAX_AUDIO_BYTES = 12 * 1024 * 1024
_NUMBER_WORDS = {'zero': '0', 'one': '1', 'two': '2', 'three': '3', 'four': '4', 'five': '5', 'six': '6',
                 'seven': '7', 'eight': '8', 'nine': '9', 'ten': '10'}


CHUNK_SECONDS = 28  # Voicebox's /transcribe only returns the first ~30 s of a longer recording


def _split(audio: bytes) -> List[bytes]:
    """Cuts a recording into pieces short enough for Voicebox to transcribe in
    full (confirmed live: a 119 s reading came back as only its first 48 words).
    Re-encoded rather than stream-copied, so every piece starts cleanly."""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / 'in.m4a'
        src.write_bytes(audio)
        probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', str(src)],
                               capture_output=True, text=True, timeout=30)
        try:
            duration = float(probe.stdout.strip())
        except ValueError:
            return [audio]
        if duration <= CHUNK_SECONDS + 2:
            return [audio]
        subprocess.run(['ffmpeg', '-v', 'error', '-i', str(src), '-f', 'segment', '-segment_time', str(CHUNK_SECONDS),
                        '-c:a', 'aac', '-b:a', '64k', str(Path(tmp) / 'part%03d.m4a')], check=True, timeout=120)
        return [p.read_bytes() for p in sorted(Path(tmp).glob('part*.m4a'))]


async def transcribe_all(audio: bytes, stt_model: str, voicebox_url: str) -> str:
    parts = await asyncio.to_thread(_split, audio)
    texts = []
    for part in parts:
        texts.append((await _transcribe(part, stt_model, voicebox_url) or '').strip())
    return ' '.join(t for t in texts if t)


def _proper_nouns(text: str) -> set:
    """Words written with a capital letter in the middle of a sentence: names,
    which speech-to-text often spells differently (Cozmo -> Cosmo)."""
    names = set()
    for m in re.finditer(r"(?<![.!?]\s)(?<!^)\b([A-Z][a-zA-Z']+)", text):
        before = text[:m.start()].rstrip()
        if before and before[-1] not in '.!?"\u201C':
            names.add(m.group(1).lower().replace("'s", '').rstrip("'"))
    return names


def _close(a: str, b: str) -> bool:
    return SequenceMatcher(a=a, b=b).ratio() >= 0.6


def _words(text: str) -> List[str]:
    out = []
    for w in re.findall(r"[A-Za-z0-9']+", text.lower().replace('’', "'")):
        w = _NUMBER_WORDS.get(w, w)
        out.append(w)
    return out


def running_record(passage: str, transcript: str, seconds: Optional[float]) -> Dict[str, Any]:
    """Aligns what he said with the passage. Only the part of the passage up to
    the last word he matched counts as attempted, so stopping early is reported
    as 'stopped early', not as dozens of mistakes."""
    target, said = _words(passage), _words(transcript)
    names = _proper_nouns(passage)
    ops = SequenceMatcher(a=target, b=said, autojunk=False).get_opcodes()
    last_matched = max((a2 - 1 for op, a1, a2, b1, b2 in ops if op == 'equal'), default=-1)
    attempted = last_matched + 1
    correct, skipped, changed, extra, names_heard = 0, [], [], [], []
    for op, a1, a2, b1, b2 in ops:
        if a1 >= attempted and op != 'insert':
            continue  # after the last word he matched: he just didn't get there
        if op == 'equal':
            correct += a2 - a1
        elif op == 'delete':
            skipped.append(' '.join(target[a1:a2]))
        elif op == 'replace':
            exp, got = target[a1:a2], said[b1:b2]
            # A name heard slightly differently (Cozmo/Cosmo, Seal/sail) is the
            # speech-to-text's spelling, not a reading mistake.
            if len(exp) == len(got) and all(e.replace("'s", '').rstrip("'") in names and _close(e, g)
                                             for e, g in zip(exp, got)):
                correct += len(exp)
                names_heard.append({'expected': ' '.join(exp), 'heard': ' '.join(got)})
            else:
                changed.append({'expected': ' '.join(exp), 'said': ' '.join(got)})
        elif op == 'insert':
            extra.append(' '.join(said[b1:b2]))
    errors_in_attempted = len(skipped) + len(changed)
    accuracy = round(100.0 * correct / attempted, 1) if attempted else 0.0
    wcpm = round(correct * 60.0 / seconds) if seconds and seconds >= 5 else None
    return {
        'passageWords': len(target), 'wordsAttempted': attempted, 'wordsCorrect': correct,
        'accuracy': accuracy, 'wordsCorrectPerMinute': wcpm,
        'skipped': skipped[:20], 'changed': changed[:20], 'extra': extra[:20], 'namesHeardDifferently': names_heard[:20],
        'errorCount': errors_in_attempted, 'stoppedEarly': attempted < len(target) * 0.9,
    }


class StageCheck(BaseModel):
    stage: Literal['understand', 'plan', 'number_sentence', 'solve', 'check']
    status: Literal['did', 'partly', 'missing']
    evidence: str = Field(description="A short exact quote from what the child said that shows this, or '' if missing.")


class ApproachCheck(BaseModel):
    stages: List[StageCheck]
    operation_said: str = Field(description="The operation the child said they used: add, take away, times, share/divide, or 'not said'.")
    operation_right: Optional[bool] = Field(description="Whether that operation fits the problem; null if not said.")
    strategy: str = Field(description="How they worked it out, in a few words, e.g. 'column subtraction with exchange', 'counted on', 'guessed', 'not said'.")
    for_parent: str = Field(description="At most two short plain sentences (under 40 words) for the parent: what the child understood and the one thing to work on.")
    for_child: str = Field(description="One warm, specific sentence of praise for the child, at most 20 words.")


async def _check_explanation(transcript: str, prompt: str, answer_key: str, given: str,
                             operation: str) -> Dict[str, Any]:
    cfg = await config_sdk.get_stage_config(AGENT_ID, 'check_approach',
                                            load_runtime_config(AGENT_CODE_DIR)['check_approach'])
    text = (
        "A child (about 9) solved a maths word problem and then explained out loud how they solved it.\n\n"
        f"Problem: {prompt}\n"
        f"Correct answer: {answer_key or 'not given'}\n"
        f"Operation that fits: {operation or 'work it out from the problem'}\n"
        f"Child's typed answer: {given or 'not given'}\n"
        f'What the child said (speech-to-text, may have small errors): """{transcript}"""\n\n'
        "Judge ONLY from what the child said. For each step - understand (said what the question asks), "
        "plan (chose an operation and ideally why), number_sentence (said the calculation, e.g. 46 take away 29), "
        "solve (worked it out), check (checked or said it makes sense) - mark did, partly or missing, quoting "
        "their words as evidence. Be fair to a child: everyday words count ('I took away' = subtraction). "
        "Do not invent anything they didn't say."
    )
    result = await _agent.ask(text, stage='check_approach', model=cfg['model'], temperature=cfg['temperature'],
                              schema=ApproachCheck, think=False)
    out = result.model_dump()
    weights = {'did': 1.0, 'partly': 0.5, 'missing': 0.0}
    out['score'] = sum(weights[s['status']] for s in out['stages'])
    out['outOf'] = 5
    return out


async def run(audio_b64: str, mode: str = 'explanation', prompt: str = '', answer_key: str = '',
              given: str = '', operation: str = '', passage_text: str = '', seconds: Optional[float] = None,
              language: str = 'en') -> Dict[str, Any]:
    if mode not in ('explanation', 'reading'):
        raise ValueError("mode must be 'explanation' or 'reading'.")
    try:
        audio = base64.b64decode(audio_b64 or '', validate=True)
    except Exception:
        raise ValueError('audio_b64 is not valid base64.')
    if not audio:
        raise ValueError('audio_b64 was empty.')
    if len(audio) > MAX_AUDIO_BYTES:
        raise ValueError('That recording is too long.')

    # AdiyanReader's own settings for Voicebox and its Whisper model, so both agents
    # transcribe the same way and one dashboard change applies to both.
    voicebox_url = await config_sdk.get_constant('adiyan_reader', 'voicebox_url', VOICEBOX_URL)
    stt_model = await config_sdk.get_constant(
        AGENT_ID, 'stt_model', _DEFAULT_STT_MODEL,
        description="Voicebox Whisper model for the child's recordings (base, small, medium, large, turbo).",
    )
    transcript = await transcribe_all(audio, stt_model, voicebox_url)
    result: Dict[str, Any] = {'mode': mode, 'transcript': transcript, 'sttModel': stt_model}
    if not transcript:
        result['heard'] = False
        return result
    result['heard'] = True

    if mode == 'reading':
        if not passage_text.strip():
            raise ValueError('reading mode needs passage_text.')
        result['runningRecord'] = running_record(passage_text, transcript, seconds)
        # AdiyanReader's own meaning check, with its own model and settings.
        eval_cfg = await config_sdk.get_stage_config(
            'adiyan_reader', 'eval_audio_quality', {'model': 'gemma4:e2b', 'temperature': 0.1, 'base_url': None})
        result['meaningMatch'] = await _score_match(passage_text, transcript, eval_cfg)
    else:
        result['approach'] = await _check_explanation(transcript, prompt, answer_key, given, operation)
    return result
