"""
Shared vision-reasoning stages, same category as skill_router.py: LLM
reasoning any agent can import directly, not a side-effecting MCP tool. An
image arriving over WhatsApp can mean more than one thing (a contact card to
register, content to hand to Knowledge Bank once that agent exists, or
neither) - deciding which is a routing decision, and routing decisions live
in Orchestrator (mesh/orchestrator/router.py already does this for text),
never in mesh/mcp/whatsapp/, which only knows WhatsApp send/receive
mechanics. classify_image() is that decision's vision-model equivalent of
skill_router.classify(); describe_contact_image() only runs once classify
has already decided the image is a contact card.

qwen3-vl:8b, not a second local model - same qwen3 family already used for
every other LLM stage in this system, already pulled locally, confirmed
capable of reading a synthetic name+phone screenshot live before this was
wired in.
"""
from pathlib import Path
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel

from mesh.lib import config_sdk
from mesh.lib.agent_sdk import AdiyanAgent
from mesh.lib.config import load_seed_config

VISION_MODEL = 'qwen3-vl:8b'
OLLAMA_URL = 'http://localhost:11434'
TEMPERATURE = 0.2

# Shared platform prompts, same _tool_resolution/_skill_router pseudo
# agent_id convention - vision classification is genuinely the same
# decision regardless of which agent's flow the image arrived through.
_SHARED_AGENT_ID = '_vision'
_agent = AdiyanAgent(_SHARED_AGENT_ID)
_SEED = load_seed_config(Path(__file__).parent)


def _seeded(key: str) -> Dict[str, Any]:
    return _SEED.get(key, {'value': '', 'description': ''})


ImagePurpose = Literal['contact_card', 'knowledge_content', 'unclear']


class _PurposeChoice(BaseModel):
    purpose: ImagePurpose


async def classify_image(image_b64: str, mimetype: str) -> ImagePurpose:
    """The routing decision itself - what should happen with this image.
    Kept separate from describe_contact_image() the same way skill_router's
    classify()/extract() stay separate: narrowing the follow-up prompt to
    just the decided purpose beats one do-everything prompt."""
    seeded = _seeded('vision_classify_prompt')
    prompt = await config_sdk.get_constant(
        _SHARED_AGENT_ID, 'vision_classify_prompt', seeded['value'], description=seeded['description'],
    )
    choice = await _agent.ask(
        prompt, stage='classify_image', model=VISION_MODEL, temperature=TEMPERATURE,
        schema=_PurposeChoice, image_b64=image_b64, image_mimetype=mimetype,
    )
    return choice.purpose


async def describe_contact_image(image_b64: str, mimetype: str) -> Optional[str]:
    """None if the image doesn't actually contain a readable name+phone pair
    after all (classify_image can be wrong) - the caller should not forward
    anything to Orchestrator's registration flow in that case. Otherwise a
    short imperative sentence ready to feed straight into the same
    add_named_contact classify/extract stage a typed admin command goes
    through."""
    seeded = _seeded('vision_caption_prompt')
    prompt = await config_sdk.get_constant(
        _SHARED_AGENT_ID, 'vision_caption_prompt', seeded['value'], description=seeded['description'],
    )
    caption = (await _agent.ask(
        prompt, stage='describe_contact_image', model=VISION_MODEL, temperature=TEMPERATURE,
        image_b64=image_b64, image_mimetype=mimetype,
    ) or '').strip()
    if not caption or caption.upper() == 'NONE':
        return None
    return caption
