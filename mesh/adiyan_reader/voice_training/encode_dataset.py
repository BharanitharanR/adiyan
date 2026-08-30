"""Turns manifest.jsonl (from prepare_dataset.py) into the exact input_ids
format Orpheus fine-tuning expects.

This is not a guess at the format - it's copied logic-for-logic from
Canopy Labs' own data-prep notebook (the one linked from canopyai/
Orpheus-TTS's README under "Finetune Model" step 2:
https://colab.research.google.com/drive/1wg_CPCA-MzsWtsujwy-1Ovhv-tn8Q1nD),
adapted to run locally against our own manifest instead of a
Hugging-Face-hosted raw dataset. Every special-token id and the SNAC
7-frame interleaving order below match that notebook's tokenise_audio()
and create_input_ids() cells exactly - this is what makes the resulting
input_ids decodable by the same SNAC model at inference time
(mesh/adiyan_reader/tts.py's own decode already assumes this interleaving).

Their README's own guidance: quality starts to show at ~50 examples,
~300/speaker recommended for best results. This dataset has 69 examples
for one speaker (fewer than ideal) - a real, disclosed limitation, not
hidden from the training run.

"{speaker}: {text}" is their own documented multispeaker prompt format
(README's "Prompting" section), not invented here - it's what actually
lets `voice="bharani"` be selected the same way `voice="tara"` already is.

Usage: python3 -m mesh.adiyan_reader.voice_training.encode_dataset
"""
import json
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
import torchaudio.transforms as T
from snac import SNAC
from transformers import AutoTokenizer

MANIFEST_PATH = Path.home() / '.Adiyan' / 'voice_training' / 'bharani' / 'manifest.jsonl'
OUT_PATH = Path.home() / '.Adiyan' / 'voice_training' / 'bharani' / 'encoded_dataset.jsonl'
BASE_MODEL_DIR = Path(__file__).parent / 'models' / 'orpheus-3b-0.1-ft'
TARGET_SAMPLE_RATE = 24000

# Verbatim from Canopy Labs' own data-prep notebook.
TOKENISER_LENGTH = 128256
START_OF_TEXT = 128000
END_OF_TEXT = 128009
START_OF_SPEECH = TOKENISER_LENGTH + 1
END_OF_SPEECH = TOKENISER_LENGTH + 2
START_OF_HUMAN = TOKENISER_LENGTH + 3
END_OF_HUMAN = TOKENISER_LENGTH + 4
START_OF_AI = TOKENISER_LENGTH + 5
END_OF_AI = TOKENISER_LENGTH + 6
PAD_TOKEN = TOKENISER_LENGTH + 7
AUDIO_TOKENS_START = TOKENISER_LENGTH + 10


def tokenise_audio(waveform: torch.Tensor, orig_sample_rate: int, snac_model: SNAC, device: str) -> list:
    """Verbatim from the notebook's tokenise_audio(), generalized to accept
    whatever sample rate the source WAV actually has (ours are already
    24kHz from prepare_dataset.py, so this resample is a no-op in
    practice, but kept so this stays correct if that ever changes)."""
    if waveform.dim() == 1:
        waveform = waveform.unsqueeze(0)
    waveform = waveform.to(dtype=torch.float32)
    if orig_sample_rate != TARGET_SAMPLE_RATE:
        waveform = T.Resample(orig_freq=orig_sample_rate, new_freq=TARGET_SAMPLE_RATE)(waveform)
    waveform = waveform.unsqueeze(0).to(device)

    with torch.inference_mode():
        codes = snac_model.encode(waveform)

    all_codes = []
    for i in range(codes[0].shape[1]):
        all_codes.append(codes[0][0][i].item() + 128266)
        all_codes.append(codes[1][0][2 * i].item() + 128266 + 4096)
        all_codes.append(codes[2][0][4 * i].item() + 128266 + (2 * 4096))
        all_codes.append(codes[2][0][(4 * i) + 1].item() + 128266 + (3 * 4096))
        all_codes.append(codes[1][0][(2 * i) + 1].item() + 128266 + (4 * 4096))
        all_codes.append(codes[2][0][(4 * i) + 2].item() + 128266 + (5 * 4096))
        all_codes.append(codes[2][0][(4 * i) + 3].item() + 128266 + (6 * 4096))
    return all_codes


def remove_duplicate_frames(codes_list: list) -> list:
    """Verbatim from the notebook's remove_duplicate_frames() - collapses
    consecutive identical 7-token frames (silence/held phonemes), which
    otherwise waste sequence length on near-empty audio like the pauses in
    Batch63/66/610."""
    if len(codes_list) % 7 != 0:
        raise ValueError('codes_list length must be divisible by 7')
    result = codes_list[:7]
    for i in range(7, len(codes_list), 7):
        if codes_list[i] != result[-7]:
            result.extend(codes_list[i:i + 7])
    return result


def create_input_ids(text: str, codes_list: list, tokenizer) -> dict:
    """Verbatim from the notebook's create_input_ids()."""
    text_ids = tokenizer.encode(text, add_special_tokens=True)
    text_ids.append(END_OF_TEXT)
    input_ids = (
        [START_OF_HUMAN] + text_ids + [END_OF_HUMAN]
        + [START_OF_AI] + [START_OF_SPEECH] + codes_list + [END_OF_SPEECH] + [END_OF_AI]
    )
    return {'input_ids': input_ids, 'labels': input_ids, 'attention_mask': [1] * len(input_ids)}


def main() -> None:
    device = 'mps' if torch.backends.mps.is_available() else 'cpu'
    print(f'Using device: {device}')

    snac_model = SNAC.from_pretrained('hubertsiuzdak/snac_24khz').eval().to(device)
    tokenizer = AutoTokenizer.from_pretrained(str(BASE_MODEL_DIR))

    rows = []
    with open(MANIFEST_PATH) as f:
        manifest = [json.loads(line) for line in f]

    for entry in manifest:
        audio_np, sr = sf.read(entry['audio'], dtype='float32', always_2d=False)
        waveform = torch.from_numpy(np.asarray(audio_np, dtype=np.float32))
        codes_list = tokenise_audio(waveform, sr, snac_model, device)
        codes_list = remove_duplicate_frames(codes_list)
        # "{speaker}: {text}" - Canopy Labs' own multispeaker prompt format
        # (README's Prompting section), same convention "tara:", "leo:" etc
        # already use at inference.
        prompt_text = f"{entry['speaker']}: {entry['text']}"
        example = create_input_ids(prompt_text, codes_list, tokenizer)
        example['source_audio'] = entry['audio']
        rows.append(example)
        print(f"encoded {entry['audio']} -> {len(example['input_ids'])} tokens")

    with open(OUT_PATH, 'w') as f:
        for row in rows:
            f.write(json.dumps(row) + '\n')
    print(f'Wrote {len(rows)} encoded examples to {OUT_PATH}')


if __name__ == '__main__':
    main()
