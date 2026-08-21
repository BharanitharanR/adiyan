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
from typing import Literal, Optional

from langchain_core.messages import HumanMessage
from langchain_ollama import ChatOllama
from pydantic import BaseModel

VISION_MODEL = 'qwen3-vl:8b'
OLLAMA_URL = 'http://localhost:11434'
TEMPERATURE = 0.2

ImagePurpose = Literal['contact_card', 'knowledge_content', 'unclear']


class _PurposeChoice(BaseModel):
    purpose: ImagePurpose


_CLASSIFY_PROMPT = (
    'Look at this image and decide what it is:\n'
    '- contact_card: a WhatsApp contact card, or a screenshot of a phone '
    "contact entry, showing a person's name and phone number.\n"
    '- knowledge_content: anything else worth remembering or storing - a '
    'document, receipt, note, photo, screenshot of text, a page from a '
    'book, a whiteboard, etc.\n'
    "- unclear: neither of the above, or the image doesn't clearly fit.\n"
    'Choose exactly one.'
)

_CAPTION_PROMPT = (
    "This image is a WhatsApp contact card or a screenshot of one. "
    "If it clearly shows a person's name and phone number, reply with exactly one line: "
    '"Add <name> as a client, their number is <phone number>". '
    'Use only what is actually visible - do not guess a missing name or number. '
    'If no name+phone pair is clearly visible in the image, reply with exactly: NONE.'
)


def _image_message(prompt: str, image_b64: str, mimetype: str) -> HumanMessage:
    return HumanMessage(content=[
        {'type': 'text', 'text': prompt},
        {'type': 'image_url', 'image_url': f'data:{mimetype};base64,{image_b64}'},
    ])


async def classify_image(image_b64: str, mimetype: str) -> ImagePurpose:
    """The routing decision itself - what should happen with this image.
    Kept separate from describe_contact_image() the same way skill_router's
    classify()/extract() stay separate: narrowing the follow-up prompt to
    just the decided purpose beats one do-everything prompt."""
    model = ChatOllama(
        model=VISION_MODEL, base_url=OLLAMA_URL, temperature=TEMPERATURE,
    ).with_structured_output(_PurposeChoice)
    choice = await model.ainvoke([_image_message(_CLASSIFY_PROMPT, image_b64, mimetype)])
    return choice.purpose


async def describe_contact_image(image_b64: str, mimetype: str) -> Optional[str]:
    """None if the image doesn't actually contain a readable name+phone pair
    after all (classify_image can be wrong) - the caller should not forward
    anything to Orchestrator's registration flow in that case. Otherwise a
    short imperative sentence ready to feed straight into the same
    add_named_contact classify/extract stage a typed admin command goes
    through."""
    model = ChatOllama(model=VISION_MODEL, base_url=OLLAMA_URL, temperature=TEMPERATURE)
    result = await model.ainvoke([_image_message(_CAPTION_PROMPT, image_b64, mimetype)])
    caption = (result.content or '').strip()
    if not caption or caption.upper() == 'NONE':
        return None
    return caption
