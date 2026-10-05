"""
socratic_nudge's real body. Builds a hint-only prompt for one practice question,
asks the local model (never compute_share: no `community` argument, and a
schema call is always local anyway), and checks the result before returning
it. A hint that gives the answer away, or is too long, is retried once and
otherwise refused - the Daily Practice app then shows the written hint from
the sheet instead, so a bad model answer never reaches the child.

No permission or device-key check here: agent_executor.py did that first.
"""
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from mesh.genie.constants import AGENT_ID
from mesh.lib import config_sdk
from mesh.lib.agent_sdk import AdiyanAgent
from mesh.lib.config import load_runtime_config, load_seed_config

AGENT_CODE_DIR = Path(__file__).parent.parent
_SEED = load_seed_config(AGENT_CODE_DIR)
_agent = AdiyanAgent(AGENT_ID)

_URL = re.compile(r'https?://|www\.|\S+@\S+\.\w+', re.I)


class Hint(BaseModel):
    hint: str


async def _constant(key: str) -> Any:
    seeded = _SEED.get(key, {'value': '', 'description': ''})
    return await config_sdk.get_constant(AGENT_ID, key, seeded['value'], description=seeded['description'])


def _squash(s: str) -> str:
    return re.sub(r'\s+', '', str(s)).lower()


def gives_away(hint: str, answer_key: str) -> bool:
    """True if the hint contains the answer. Numbers must match as whole
    numbers (so the answer 2 doesn't block "12"); words and phrases match
    anywhere, ignoring case and spaces."""
    answer = (answer_key or '').strip()
    if not answer:
        return False
    if re.fullmatch(r'[\d\s./r]+', answer, re.I):
        parts = [p for p in re.split(r'\s*r\s*|\s+', answer, flags=re.I) if p]
        if len(parts) > 1 and all(re.search(rf'(?<!\d)(?<!\d\.){re.escape(p)}(?!\d)(?!\.\d)', hint) for p in parts):
            return True
        return re.search(rf'(?<!\d)(?<!\d\.){re.escape(answer)}(?!\d)(?!\.\d)', hint) is not None \
            or (len(_squash(answer)) > 2 and _squash(answer) in _squash(hint))
    return _squash(answer) in _squash(hint)


def _problem(hint: str, answer_key: str, max_chars: int) -> Optional[str]:
    if not hint:
        return 'empty'
    if len(hint) > max_chars:
        return 'too long'
    if _URL.search(hint):
        return 'has a link'
    if gives_away(hint, answer_key):
        return 'gives the answer away'
    return None


async def run(prompt: str, answer_key: str = '', qtype: str = 'question', wish: int = 1,
              attempt: str = '', options: Optional[List[str]] = None, picture: str = '',
              stage: str = '', previous_hints: Optional[List[str]] = None,
              grade: int = 4, age: int = 9, locale: str = 'en-IN') -> Dict[str, Any]:
    prompt = (prompt or '').strip()
    if not prompt:
        raise ValueError('prompt (the question) was empty.')
    wish = 2 if int(wish or 1) >= 2 else 1
    cfg = await config_sdk.get_stage_config(AGENT_ID, 'hint', load_runtime_config(AGENT_CODE_DIR)['hint'])
    max_chars = int(await _constant('max_chars') or 220)

    extra = ''
    if options:
        extra += 'Options: ' + ' | '.join(str(o) for o in options) + '\n'
    if picture:
        extra += f'Picture: {picture}\n'
    if stage:
        extra += f'The child is on this step of solving it: {stage}\n'
    fields = dict(
        grade=grade, age=age, locale=locale, qtype=qtype, prompt=prompt, extra=extra,
        answer_key=answer_key or '(not given)', attempt=attempt or '(nothing yet)',
        previous='; '.join(previous_hints or []) or '(none)',
        max_words=await _constant('max_words') or 25,
        wish_rule=await _constant(f'wish_{wish}_rule'),
    )
    template = await _constant('hint_prompt_template')
    try:
        text = template.format(**fields)
    except Exception:
        text = _SEED['hint_prompt_template']['value'].format(**fields)

    started = time.monotonic()
    problem = None
    for attempt_no in range(2):
        # think=False, as AdiyanReader's eval does: one short bounded answer gains nothing from a
        # reasoning trace, and skipping it is what keeps a hint fast enough for a child waiting on it.
        result = await _agent.ask(text, stage='hint', model=cfg['model'], temperature=cfg['temperature'],
                                  schema=Hint, think=False)
        hint = re.sub(r'^[-*•\s"]+|["\s]+$', '', ' '.join((result.hint or '').split()))  # no list markers or quotes
        problem = _problem(hint, answer_key, max_chars)
        if problem is None:
            return {'hint': hint, 'wish': wish, 'source': 'adiyan', 'model': cfg['model'],
                    'ms': int((time.monotonic() - started) * 1000)}
        text += f'\n\nYour last hint was rejected because it {problem}. Write a different one that follows every rule.'
    return {'hint': None, 'wish': wish, 'source': 'adiyan', 'model': cfg['model'], 'refused': problem,
            'ms': int((time.monotonic() - started) * 1000)}
