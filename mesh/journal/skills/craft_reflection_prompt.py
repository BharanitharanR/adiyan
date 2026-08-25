"""
craft_reflection_prompt's real body. Calls Memory Agent first; if it has
real snippets, crafts a personalized question grounded only in what was
actually retrieved. If Memory Agent has nothing (exactly the case for
target='self' today - see mesh/journal's design discussion), falls back to
an honest, clearly-generic prompt rather than inventing personal details
that don't exist. Never the reverse - personalization is only ever built
from real retrieved snippets, never guessed at when snippets are empty.
"""
from pathlib import Path
from typing import Any, Dict, Optional

from langchain_ollama import ChatOllama
from pydantic import BaseModel

from mesh.journal.constants import AGENT_ID, MEMORY_AGENT_URL
from mesh.lib import config_sdk, permissions
from mesh.lib.a2a_client import call_agent
from mesh.lib.config import load_runtime_config

AGENT_CODE_DIR = Path(__file__).parent.parent
OLLAMA_URL = 'http://localhost:11434'


class ReflectionPrompt(BaseModel):
    question: str


async def _craft_personalized(theme: Optional[str], snippets: list, cfg: Dict[str, Any]) -> str:
    model = ChatOllama(
        model=cfg['model'], base_url=OLLAMA_URL, temperature=cfg['temperature'],
    ).with_structured_output(ReflectionPrompt)
    result = await model.ainvoke(
        'Here is what is actually known about this person recently, from their own past conversations:\n\n'
        + '\n'.join(f'- {s}' for s in snippets)
        + f'\n\nTheme to focus on: {theme or "whatever seems most relevant from the above"}\n\n'
        'Write ONE tailored journaling reflection question. Reference something '
        'specific and true from what is listed above - do not invent details '
        'that are not actually there.'
    )
    return result.question


async def _craft_generic(theme: Optional[str], cfg: Dict[str, Any]) -> str:
    model = ChatOllama(
        model=cfg['model'], base_url=OLLAMA_URL, temperature=cfg['temperature'],
    ).with_structured_output(ReflectionPrompt)
    result = await model.ainvoke(
        'Write ONE warm, general journaling reflection question for someone '
        'writing tonight. Nothing is known about this specific person, so do '
        f'not reference any personal detail as if it were true.\n\n'
        f'Theme (if any): {theme or "no specific theme - keep it open-ended"}'
    )
    return result.question


async def run(contact_name: str, theme: Optional[str] = None) -> Dict[str, Any]:
    cfg = await config_sdk.get_stage_config(
        AGENT_ID, 'craft_prompt', load_runtime_config(AGENT_CODE_DIR)['craft_prompt'],
    )

    try:
        # Service token - the caller's own right to craft_reflection_prompt
        # was already checked at the agent_executor boundary.
        token = permissions.mint_token('journal', 'service')
        memory_agent_url = await config_sdk.get_constant(
            AGENT_ID, 'memory_agent_url', MEMORY_AGENT_URL,
            description='URL of the Memory Agent this checks with for past conversation snippets before writing a reflection prompt.',
        )
        memory_result = await call_agent(memory_agent_url, 'recall_contact_memory', {
            'contact_name': contact_name,
            'query': theme or 'recent thoughts, mood, and challenges',
            'top_k': 3,
        }, token=token)
    except RuntimeError:
        # Memory Agent unreachable/erroring - same "degrade, don't fail"
        # treatment as an empty result, not a hard failure of this skill.
        memory_result = {'snippets': [], 'available': False}

    snippets = memory_result.get('snippets', [])
    if snippets:
        question = await _craft_personalized(theme, snippets, cfg)
        personalized = True
    else:
        question = await _craft_generic(theme, cfg)
        personalized = False

    return {'question': question, 'personalized': personalized}
