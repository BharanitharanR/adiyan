"""
Turns a target agent's raw structured result into a short, natural reply.
Ported from mesh/whatsapp_connector/humanize.py (now retired) - same logic.
Every mesh/ agent returns Part.data (structured JSON) meant for a machine
caller; this is where that gets turned into words for a human, grounded
only in what the result dict actually contains.

The prompt template itself is now Mongo-backed via config_sdk (this
module's own pilot for migrating actual prompt text, not just
model/temperature/timeout, off this codebase's hardcoded default and onto
the central config SDK) - _DEFAULT_PROMPT_TEMPLATE below is only the
first-seed value and the fallback if Mongo is unreachable or the on-file
template is malformed, never the value actually used once Mongo has one on
file.
"""
import logging
from typing import Any, Dict

from langchain_ollama import ChatOllama
from pydantic import BaseModel

from mesh.lib import config_sdk
from mesh.orchestrator.constants import AGENT_ID

logger = logging.getLogger('Humanize')

OLLAMA_URL = 'http://localhost:11434'

_DEFAULT_PROMPT_TEMPLATE = (
    'The user said: "{original_message}"\n\n'
    'Here is the structured result from handling their request: {result}\n\n'
    'Write a short, natural WhatsApp reply based only on what is in this '
    'result - do not invent anything not present in it.'
)


class HumanReply(BaseModel):
    text: str


async def humanize(original_message: str, result: Dict[str, Any], cfg: Dict[str, Any]) -> str:
    model = ChatOllama(
        model=cfg['model'], base_url=OLLAMA_URL, temperature=cfg['temperature'],
    ).with_structured_output(HumanReply)

    template = await config_sdk.get_constant(AGENT_ID, 'humanize_prompt_template', _DEFAULT_PROMPT_TEMPLATE)
    try:
        prompt = template.format(original_message=original_message, result=result)
    except Exception as e:
        # A template edited (via the future WhatsApp/dashboard tool) into
        # something that doesn't actually have {original_message}/{result}
        # placeholders must not break every reply in the mesh - fall back
        # to the known-good default rather than erroring out.
        logger.warning(f'humanize_prompt_template on file is malformed, using default: {e}')
        prompt = _DEFAULT_PROMPT_TEMPLATE.format(original_message=original_message, result=result)

    reply = await model.ainvoke(prompt)
    return reply.text
