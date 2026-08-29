"""
Turns a target agent's raw structured result into a short, natural reply.
Ported from mesh/whatsapp_connector/humanize.py (now retired) - same logic.
Every mesh/ agent returns Part.data (structured JSON) meant for a machine
caller; this is where that gets turned into words for a human, grounded
only in what the result dict actually contains.

The prompt template itself is Mongo-backed via config_sdk, seeded from
mesh/orchestrator/seed_config.json (not a hardcoded Python literal here) -
see mesh/lib/config_sdk.py's seed_from_file() and
mesh/analysis/skills/analyze.py's own _seeded() for the same pattern
applied agent-wide. The seed file's value is only the first-seed value and
the fallback if Mongo is unreachable or the on-file template is malformed,
never the value actually used once Mongo has one on file.
"""
import logging
from pathlib import Path
from typing import Any, Dict

from langchain_ollama import ChatOllama
from pydantic import BaseModel

from mesh.lib import config_sdk
from mesh.lib.config import load_seed_config
from mesh.orchestrator.constants import AGENT_ID

logger = logging.getLogger('Humanize')

OLLAMA_URL = 'http://localhost:11434'

_SEED = load_seed_config(Path(__file__).parent)


def _seeded(key: str) -> Dict[str, Any]:
    return _SEED.get(key, {'value': '', 'description': ''})


class HumanReply(BaseModel):
    text: str


async def humanize(original_message: str, result: Dict[str, Any], cfg: Dict[str, Any]) -> str:
    model = ChatOllama(
        model=cfg['model'], base_url=OLLAMA_URL, temperature=cfg['temperature'],
    ).with_structured_output(HumanReply)

    seeded = _seeded('humanize_prompt_template')
    template = await config_sdk.get_constant(
        AGENT_ID, 'humanize_prompt_template', seeded['value'], description=seeded['description'],
    )
    try:
        prompt = template.format(original_message=original_message, result=result)
    except Exception as e:
        # A template edited (via the WhatsApp/dashboard tool) into
        # something that doesn't actually have {original_message}/{result}
        # placeholders must not break every reply in the mesh - fall back
        # to the known-good seed default rather than erroring out.
        logger.warning(f'humanize_prompt_template on file is malformed, using seed default: {e}')
        prompt = seeded['value'].format(original_message=original_message, result=result)

    reply = await model.ainvoke(prompt)
    return reply.text
