"""
Local, free text-to-speech via the Orpheus model, served through this
deployment's own Ollama instance (a separate model, legraphista/Orpheus:
3b-ft-q4_k_m, from the qwen3 models every other agent uses for reasoning -
Ollama loads/unloads it on demand like any other model, no second inference
engine needed).

Adapted from mesh/voice/Orpheus-TTS-Local/TTS.py's own generate() - that
script is a raw CLI tool (argparse, an interactive input loop, local audio
playback via sounddevice) built for a person running it by hand at a
terminal, not something an A2A agent calls headlessly. This module keeps
only the actual synthesis path (Ollama call -> SNAC decode -> audio bytes),
made async (httpx, matching every other agent's own Ollama calls) instead
of TTS.py's synchronous requests.Session, with model/voice/generation
params passed in as an explicit cfg dict (config_sdk-driven at the call
site - mesh/adiyan_reader/skills/read_next_page.py) instead of argparse
flags or module-level globals.

Confirmed live this session: Ollama's /api/generate chat-templates the
prompt by default, which breaks Orpheus's expected raw
"<|audio|>voice: text<|eot_id|>" format entirely - the model replies with
ordinary conversational text instead of the <custom_token_N> audio codes
SNAC needs to decode. raw=True on every request here is not optional; the
same bug was patched into the cloned TTS.py script directly too.

Output is Opus-encoded OGG, not the raw WAV Orpheus/SNAC produce - OpenWA's
own send-audio docs are explicit that a WhatsApp voice note (PTT - the mic
bubble + waveform UI) needs audio/ogg;codecs=opus for reliable playback,
confirmed live: ffmpeg transcodes the WAV losslessly-enough for speech in
under 100ms, negligible next to the actual TTS generation time.
"""
import asyncio
import logging
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger('AdiyanReaderTTS')

SPECIAL_START = '<|audio|>'
SPECIAL_END = '<|eot_id|>'
CUSTOM_TOKEN_PREFIX = '<custom_token_'
SAMPLE_RATE = 24000

VOICES = ('tara', 'leah', 'jess', 'leo', 'dan', 'mia', 'zac', 'zoe')
DEFAULT_VOICE = 'tara'

_snac_model = None
_snac_device: Optional[str] = None


def _ensure_snac():
    """Lazy singleton, loaded on first real call - torch/snac are heavy
    imports (multi-second load, matching this codebase's own established
    pattern for Docling/LlamaIndex in mesh/memory/memory_index.py), no
    reason to pay that cost just for `import mesh.adiyan_reader.tts` to
    succeed (a syntax check, a different skill's import chain, etc.)."""
    global _snac_model, _snac_device
    if _snac_model is not None:
        return _snac_model, _snac_device

    import torch
    from snac import SNAC

    device = 'cuda' if torch.cuda.is_available() else 'mps' if torch.backends.mps.is_available() else 'cpu'
    model = SNAC.from_pretrained('hubertsiuzdak/snac_24khz').eval().to(device)
    _snac_model, _snac_device = model, device
    logger.info(f'SNAC model loaded on {device}')
    return _snac_model, _snac_device


def _turn_token_into_id(token_string: str, index: int) -> Optional[int]:
    token_string = token_string.strip()
    last_token_start = token_string.rfind(CUSTOM_TOKEN_PREFIX)
    if last_token_start == -1:
        return None
    last_token = token_string[last_token_start:]
    if not (last_token.startswith(CUSTOM_TOKEN_PREFIX) and last_token.endswith('>')):
        return None
    try:
        number_str = last_token[len(CUSTOM_TOKEN_PREFIX):-1]
        return int(number_str) - 10 - ((index % 7) * 4096)
    except ValueError:
        return None


def _convert_frame_to_audio(multiframe: List[int]) -> Optional[bytes]:
    """One 28-token frame -> raw PCM bytes, via SNAC's 3-codebook decode.
    Verbatim logic from TTS.py's convert_to_audio() - this is SNAC's own
    codec structure (a fixed 7-tokens-per-timestep interleaving across 3
    hierarchical codebooks), not something to simplify without
    understanding the model's own token layout."""
    import torch

    if len(multiframe) < 7:
        return None
    model, device = _ensure_snac()

    codes_0: List[int] = []
    codes_1: List[int] = []
    codes_2: List[int] = []
    num_frames = len(multiframe) // 7
    frame = multiframe[:num_frames * 7]

    for j in range(num_frames):
        i = 7 * j
        codes_0.append(frame[i])
        codes_1.extend([frame[i + 1], frame[i + 4]])
        codes_2.extend([frame[i + 2], frame[i + 3], frame[i + 5], frame[i + 6]])

    codes = [
        torch.tensor(codes_0, device=device, dtype=torch.int32).unsqueeze(0),
        torch.tensor(codes_1, device=device, dtype=torch.int32).unsqueeze(0),
        torch.tensor(codes_2, device=device, dtype=torch.int32).unsqueeze(0),
    ]
    if any(bool(torch.any((c < 0) | (c > 4096))) for c in codes):
        return None

    with torch.inference_mode():
        audio_hat = model.decode(codes)

    audio_slice = audio_hat[:, :, 2048:4096].detach().cpu().numpy()
    audio_int16 = (audio_slice * 32767).astype('int16')
    return audio_int16.tobytes()


async def _generate_tokens(prompt: str, cfg: Dict[str, Any]) -> List[str]:
    """Streams raw completion tokens from Ollama for the given already-
    formatted Orpheus prompt. raw=True is the one non-negotiable flag - see
    this module's own docstring."""
    payload = {
        'model': cfg['model'],
        'prompt': prompt,
        'raw': True,
        'stream': True,
        'options': {
            'num_predict': cfg.get('max_tokens', 1200),
            'temperature': cfg.get('temperature', 0.6),
            'top_p': cfg.get('top_p', 0.9),
            'repeat_penalty': cfg.get('repetition_penalty', 1.1),
        },
    }
    tokens: List[str] = []
    async with httpx.AsyncClient(timeout=cfg.get('timeout', 300.0)) as client:
        async with client.stream('POST', f"{cfg.get('base_url', 'http://localhost:11434')}/api/generate", json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line:
                    continue
                import json
                data = json.loads(line)
                if data.get('response'):
                    tokens.append(data['response'])
                if data.get('done'):
                    break
    return tokens


def _tokens_to_wav_bytes(tokens: List[str]) -> bytes:
    buffer: List[int] = []
    count = 0
    segments: List[bytes] = []
    for token_text in tokens:
        token_id = _turn_token_into_id(token_text, count)
        if token_id is None or token_id <= 0:
            continue
        buffer.append(token_id)
        count += 1
        if count % 7 == 0 and count > 27:
            audio = _convert_frame_to_audio(buffer[-28:])
            if audio is not None:
                segments.append(audio)

    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
        wav_path = f.name
    with wave.open(wav_path, 'wb') as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(SAMPLE_RATE)
        for segment in segments:
            wav_file.writeframes(segment)

    wav_bytes = Path(wav_path).read_bytes()
    Path(wav_path).unlink(missing_ok=True)
    return wav_bytes


def _wav_to_opus_ogg(wav_bytes: bytes) -> bytes:
    """ffmpeg transcode, WAV -> Opus/OGG - see this module's own docstring
    for why WhatsApp's voice-note (PTT) UI needs this, not raw WAV."""
    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as wav_f:
        wav_f.write(wav_bytes)
        wav_path = wav_f.name
    ogg_path = wav_path.replace('.wav', '.ogg')
    try:
        subprocess.run(
            ['ffmpeg', '-y', '-i', wav_path, '-c:a', 'libopus', '-b:a', '32k', ogg_path],
            check=True, capture_output=True, timeout=30,
        )
        return Path(ogg_path).read_bytes()
    finally:
        Path(wav_path).unlink(missing_ok=True)
        Path(ogg_path).unlink(missing_ok=True)


async def synthesize(text: str, voice: str, cfg: Dict[str, Any]) -> bytes:
    """Text -> Opus/OGG audio bytes, ready for OpenWAService.send_voice().
    Raises if Orpheus produced no usable audio at all (e.g. Ollama
    unreachable, or the model genuinely emitted nothing) - callers decide
    what that means for their own domain, same contract every other tool
    call in this mesh follows."""
    if voice not in VOICES:
        logger.warning(f"Unknown voice {voice!r}, falling back to {DEFAULT_VOICE!r}")
        voice = DEFAULT_VOICE

    prompt = f'{SPECIAL_START}{voice}: {text}{SPECIAL_END}'
    tokens = await _generate_tokens(prompt, cfg)
    wav_bytes = await asyncio.to_thread(_tokens_to_wav_bytes, tokens)
    if len(wav_bytes) <= 44:  # bare WAV header, no actual audio frames written
        raise RuntimeError('Orpheus produced no audio for this text - check Ollama and the model name.')
    return await asyncio.to_thread(_wav_to_opus_ogg, wav_bytes)
