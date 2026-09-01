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

from pydantic import BaseModel

from mesh.journal.constants import AGENT_ID, MEMORY_AGENT_URL
from mesh.lib import config_sdk, permissions
from mesh.lib.a2a_client import call_agent
from mesh.lib.agent_sdk import AdiyanAgent
from mesh.lib.config import load_runtime_config, load_seed_config

AGENT_CODE_DIR = Path(__file__).parent.parent
_SEED = load_seed_config(AGENT_CODE_DIR)
_agent = AdiyanAgent(AGENT_ID)


def _seeded(key: str) -> Dict[str, Any]:
    return _SEED.get(key, {'value': '', 'description': ''})


class ReflectionPrompt(BaseModel):
    question: str


async def _craft_personalized(theme: Optional[str], snippets: list, cfg: Dict[str, Any]) -> str:
    seeded = _seeded('craft_personalized_prompt_template')
    template = await config_sdk.get_constant(
        AGENT_ID, 'craft_personalized_prompt_template', seeded['value'], description=seeded['description'],
    )
    fmt_kwargs = dict(
        snippets='\n'.join(f'- {s}' for s in snippets),
        theme=theme or 'whatever seems most relevant from the above',
    )
    try:
        prompt = template.format(**fmt_kwargs)
    except Exception:
        prompt = seeded['value'].format(**fmt_kwargs)
    result = await _agent.ask(
        prompt, stage='craft_personalized', model=cfg['model'], temperature=cfg['temperature'], schema=ReflectionPrompt,
    )
    return result.question


async def _craft_generic(theme: Optional[str], cfg: Dict[str, Any]) -> str:
    seeded = _seeded('craft_generic_prompt_template')
    template = await config_sdk.get_constant(
        AGENT_ID, 'craft_generic_prompt_template', seeded['value'], description=seeded['description'],
    )
    fmt_kwargs = dict(theme=theme or 'no specific theme - keep it open-ended')
    try:
        prompt = template.format(**fmt_kwargs)
    except Exception:
        prompt = seeded['value'].format(**fmt_kwargs)
    result = await _agent.ask(
        prompt, stage='craft_generic', model=cfg['model'], temperature=cfg['temperature'], schema=ReflectionPrompt,
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
