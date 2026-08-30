"""One-off dataset prep for Bharani's own-voice clone of Orpheus.

Not part of any running agent - this is a script you run by hand once you
have a batch of recordings, to turn raw .m4a voice notes (from WhatsApp/the
uploads folder) plus the known Voice Recording Script text into what an
Orpheus fine-tune actually wants: mono WAV at SNAC's own 24kHz
(mesh/adiyan_reader/tts.py's own SAMPLE_RATE - the fine-tune has to match
what synthesis will later decode at) and a manifest.jsonl pairing each
clip's path with its real transcript. No invented transcripts - every line
below is copied verbatim from the published Voice Recording Script
artifact, the same one Bharani read from.

This only prepares the data. The actual fine-tuning run needs a rented GPU
(see this script's own README note) - that's a separate, later step once
the dataset here is reviewed, not something this Mac runs.

Usage: python3 -m mesh.adiyan_reader.voice_training.prepare_dataset
"""
import json
import subprocess
from pathlib import Path
from typing import Dict, List, Tuple

UPLOADS_DIR = Path('/Users/bharani/.claude/uploads/409cca48-ca7c-48ec-bb29-db4dcfab171d')
OUT_DIR = Path.home() / '.Adiyan' / 'voice_training' / 'bharani'
WAV_DIR = OUT_DIR / 'wav24khz'
MANIFEST_PATH = OUT_DIR / 'manifest.jsonl'
SAMPLE_RATE = 24000  # matches mesh/adiyan_reader/tts.py's own SNAC sample rate

SPEAKER = 'bharani'

# Verbatim from the published Voice Recording Script artifact
# (https://claude.ai/code/artifact/99313b83-cbb4-474f-987d-412930e21683).
# batch -> ordered list of lines, 1-indexed to match the Batch{N}{idx}
# filenames Bharani actually sent.
SCRIPT_LINES: Dict[int, List[str]] = {
    1: [
        "One night not long after my twenty-ninth birthday, I woke up in the early hours with a feeling of absolute dread.",
        "The silence of the night, the vague outlines of the furniture in the dark room, felt strange and unfamiliar.",
        "I opened my eyes slowly, letting the first light of morning settle in before I moved at all.",
        "There is something quietly powerful about sitting still and simply noticing your own breath.",
        "The mind wanders constantly, pulling us into yesterday's regrets or tomorrow's worries.",
        "What if the only moment that has ever truly existed is the one happening right now?",
        "He walked through the old library slowly, running his fingers along the spines of forgotten books.",
        "The rain had stopped an hour ago, but the streets still held that fresh, washed smell.",
        "She realized, somewhere between the second and third chapter, that she had stopped reading the words and started living them.",
        "Every ending, if you look closely enough, is really just the quiet start of something else.",
        "The story begins on a Tuesday, though nothing about that day seemed particularly important at the time.",
        "Sometimes the most difficult chapters teach us the most, even when we can't see it yet.",
        "He closed the book gently and sat in the stillness for a long moment before speaking.",
        "There's a certain comfort in routine, in knowing exactly what the next page will bring.",
        "The old house creaked softly as the evening settled in around it.",
    ],
    2: [
        "Have you ever noticed how quiet a room feels right after someone stops talking?",
        "What would you do if you had just one more hour today?",
        "Why does time always seem to move faster on the days we enjoy the most?",
        "Do you remember the last time you read something that genuinely surprised you?",
        "Isn't it strange how a smell can bring back a memory you'd completely forgotten?",
        "Can you believe it's already been a year since we started this?",
        "What's the one book you'd recommend to absolutely everyone?",
        "Where did you leave the keys this time?",
        "How many chapters do you think are left in this book?",
        "Are you coming tonight, or should I go on ahead?",
    ],
    3: [
        "I can't believe we actually finished the whole project tonight!",
        "This is honestly one of the best things I've built all year.",
        "Wait, did that actually work? That's incredible!",
        "I'm so glad we finally got this sorted out, it feels amazing.",
        "Oh, this is going to be so much fun, I can't wait to try it.",
        "We did it! I genuinely didn't think we'd get here by tonight.",
        "That's such a good idea, why didn't I think of that earlier?",
        "Honestly, this might be my favorite part of the whole book.",
        "I love how this turned out, it's exactly what I pictured.",
        "Yes! Finally, after all that testing, it's actually working.",
    ],
    4: [
        "I sat there for a while, not really thinking about anything in particular.",
        "It's strange how some goodbyes feel bigger than others, even the small ones.",
        "There was a quiet sadness in the room that nobody wanted to name out loud.",
        "Some nights, the silence says more than any conversation could.",
        "I miss how simple things used to feel before everything got so complicated.",
        "He looked out the window for a long time before he finally spoke.",
        "Not every story gets a happy ending, and that's alright too.",
        "Sometimes you just need to sit with a feeling instead of fixing it.",
        "It took me a long time to understand what she meant by that.",
        "There's a kind of peace that only comes after you stop resisting.",
    ],
    5: [
        "Um, I think the meeting got moved to, like, three o'clock or something?",
        "Hmm, let me think about that for a second, actually.",
        "So, yeah, that's basically how the whole thing works.",
        "I mean, it's not perfect, but honestly it's pretty close.",
        "Okay so, wait, can you say that one more time?",
        "Yeah, no, I totally get what you mean.",
        "Honestly? I have no idea what happened there.",
        "Anyway, so where were we before that?",
        "Right, right, that makes a lot more sense now.",
        "Oh wait, actually, hold on, I think I misread that.",
    ],
    6: [
        "The meeting is scheduled for August twenty-sixth at nine in the morning.",
        "There were exactly four hundred and twelve pages in that first edition.",
        "Call me back at nine one nine, three six one, three one five, three seven nine.",
        "The book was originally published in nineteen ninety-seven.",
        "Chapter twelve begins on page two hundred and three.",
        "It costs about twenty-nine dollars and ninety-nine cents.",
        "Eckhart Tolle wrote this in the early two thousands.",
        "We're aiming for around one hundred and fifty recordings by tomorrow.",
        "The temperature dropped to almost zero last night.",
        "Page one, page two, page three, all the way through page ten.",
    ],
    7: [
        "When I finally sat down to read, after a long day of meetings and noise and everything in between, the silence of the room felt like a small, quiet gift.",
        "It wasn't that the plan was wrong, exactly, it was more that nobody had actually stopped to ask whether it was still the right plan for where we were now.",
        "She used to say that the best stories weren't the ones with the happiest endings, but the ones that made you feel something true, even if it was uncomfortable.",
        "By the time we reached the last chapter, I realized I'd been holding my breath without noticing, the way you do when a story finally starts to make sense.",
        "There's a particular kind of quiet that settles over a house late at night, once everyone else has gone to sleep and the only sound left is your own thoughts.",
    ],
}

# (upload filename, batch, line index within batch) - line index is None for
# an out-of-script extra clip (Batch511), which is flagged rather than
# silently mapped to a guessed line.
CLIPS: List[Tuple[str, int, int]] = [
    ('19e0544e-Batch11.m4a', 1, 1), ('3e3f75fd-Batch12.m4a', 1, 2), ('97d0e5c1-Batch13.m4a', 1, 3),
    ('d6718705-Batch14.m4a', 1, 4), ('6a7bcbd1-Batch15.m4a', 1, 5), ('1917b8f6-Batch16.m4a', 1, 6),
    ('610bf8c8-Batch17.m4a', 1, 7), ('fc770a66-Batch18.m4a', 1, 8), ('d079b2f7-Batch19.m4a', 1, 9),
    ('d292de94-Batch110.m4a', 1, 10), ('8bd14937-Batch111.m4a', 1, 11), ('4bb13c51-Batch112.m4a', 1, 12),
    ('b6b4913a-Batch113.m4a', 1, 13), ('76fa048c-Batch114.m4a', 1, 14), ('001baf57-Batch115.m4a', 1, 15),

    ('34e82e52-Batch21.m4a', 2, 1), ('424305ad-Batch22.m4a', 2, 2), ('ce28951f-Batch23.m4a', 2, 3),
    ('76930206-Batch24.m4a', 2, 4), ('0cac2891-Batch25.m4a', 2, 5), ('2a9e7c17-Batch26.m4a', 2, 6),
    ('ad23a7ae-Batch27.m4a', 2, 7), ('e80323a4-Batch28.m4a', 2, 8), ('94ca2984-Batch29.m4a', 2, 9),
    ('dc46ff92-Batch210.m4a', 2, 10),

    ('d5651b0e-Batch31.m4a', 3, 1), ('70c7a332-Batch32.m4a', 3, 2), ('2e5a0169-Batch33.m4a', 3, 3),
    ('5c04b01d-Batch34.m4a', 3, 4), ('510895ef-Batch35.m4a', 3, 5), ('47a27b42-Batch36.m4a', 3, 6),
    ('a18861d4-Batch37.m4a', 3, 7), ('d74f37ff-Batch38.m4a', 3, 8), ('205f924a-Batch39.m4a', 3, 9),
    ('a0ea4229-Batch310.m4a', 3, 10),

    ('625075e5-Batch41.m4a', 4, 1), ('600e383d-Batch42.m4a', 4, 2), ('1dab6406-Batch43.m4a', 4, 3),
    ('bbeac857-Batch44.m4a', 4, 4), ('32213827-Batch45.m4a', 4, 5), ('ee1f1f85-Batch46.m4a', 4, 6),
    ('5ba2f5b7-Batch47.m4a', 4, 7), ('05e9f427-Batch48.m4a', 4, 8), ('aa9d1df8-Batch49.m4a', 4, 9),
    ('25501202-Batch410.m4a', 4, 10),

    ('94e8b1e0-Batch51.m4a', 5, 1), ('88ff31f3-Batch52.m4a', 5, 2), ('7a09f3e5-Batch53.m4a', 5, 3),
    ('39514025-Batch54.m4a', 5, 4),
    # Batch55 was never sent - a real gap, not mapped to anything.
    ('dfdc6f44-Batch56.m4a', 5, 6),
    ('513a0597-Batch57.m4a', 5, 7), ('932b1586-Batch58.m4a', 5, 8), ('46df1752-Batch59.m4a', 5, 9),
    ('26d91f0d-Batch510.m4a', 5, 10),
    # Batch511 is an 11th clip for a 10-line batch - unconfirmed whether
    # it's the missing line 5 recorded out of order. Flagged (line=None),
    # not guessed, so a bad transcript pairing can't silently enter the
    # training set.
    ('76d62840-Batch511.m4a', 5, None),

    ('bb384848-Batch61.m4a', 6, 1), ('a39e313d-Batch62.m4a', 6, 2), ('8667a181-Batch63.m4a', 6, 3),
    ('c0541098-Batch64.m4a', 6, 4), ('c01a8e7f-Batch65.m4a', 6, 5),
    # Batch66 and Batch610 ran 25s/30.5s against ~6-9s for every other
    # single-sentence clip in this batch - very likely multiple takes in
    # one file, not a clean single reading. Included but flagged, not
    # silently treated as a normal clip - a fine-tune sample should be one
    # take of its one transcript line, not several takes concatenated.
    ('f8a9444e-Batch66.m4a', 6, 6),
    ('97ecb6fa-Batch67.m4a', 6, 7), ('30654b71-Batch68.m4a', 6, 8), ('12e00d18-Batch69.m4a', 6, 9),
    ('a92452b4-Batch610.m4a', 6, 10),

    ('e20ac231-Batch71.m4a', 7, 1), ('7422ba2f-Batch72.m4a', 7, 2), ('835ece92-Batch73.m4a', 7, 3),
    ('2e6a7ebb-Batch74.m4a', 7, 4), ('d2426286-Batch75.m4a', 7, 5),
]

# Clips whose duration (from earlier ffprobe checks) is well outside the
# ~4-17s range every clean single-sentence clip in this set fell into -
# worth a human listen/trim before they go into training, not an
# automatic exclusion.
SUSPECT_LONG = {'f8a9444e-Batch66.m4a', 'a92452b4-Batch610.m4a'}


def convert_to_wav(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ['ffmpeg', '-y', '-i', str(src), '-ac', '1', '-ar', str(SAMPLE_RATE), str(dst)],
        check=True, capture_output=True,
    )


def main() -> None:
    WAV_DIR.mkdir(parents=True, exist_ok=True)
    manifest_rows = []
    skipped = []
    flagged = []

    for filename, batch, line_idx in CLIPS:
        src = UPLOADS_DIR / filename
        if not src.exists():
            skipped.append((filename, 'file not found'))
            continue
        if line_idx is None:
            flagged.append((filename, 'unconfirmed line mapping - not included in manifest'))
            continue

        text = SCRIPT_LINES[batch][line_idx - 1]
        wav_name = f'batch{batch}_{line_idx:02d}.wav'
        wav_path = WAV_DIR / wav_name
        convert_to_wav(src, wav_path)

        row = {'audio': str(wav_path), 'text': text, 'speaker': SPEAKER}
        if filename in SUSPECT_LONG:
            row['flag'] = 'unusually long clip - listen/trim before training, may contain multiple takes'
        manifest_rows.append(row)

    with open(MANIFEST_PATH, 'w') as f:
        for row in manifest_rows:
            f.write(json.dumps(row) + '\n')

    print(f'Wrote {len(manifest_rows)} clips to {MANIFEST_PATH}')
    print(f'WAV files in {WAV_DIR}')
    if skipped:
        print(f'Skipped ({len(skipped)}): {skipped}')
    if flagged:
        print(f'Flagged, not in manifest ({len(flagged)}): {flagged}')
    suspect_in_manifest = [r['audio'] for r in manifest_rows if 'flag' in r]
    if suspect_in_manifest:
        print(f'In manifest but flagged for review: {suspect_in_manifest}')


if __name__ == '__main__':
    main()
