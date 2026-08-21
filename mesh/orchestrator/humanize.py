"""
Turns a target agent's raw structured result into a short, natural reply.
Ported from mesh/whatsapp_connector/humanize.py (now retired) - same logic,
unchanged. Every mesh/ agent returns Part.data (structured JSON) meant for a
machine caller; this is where that gets turned into words for a human,
grounded only in what the result dict actually contains.
"""
from typing import Any, Dict

from langchain_ollama import ChatOllama
from pydantic import BaseModel

OLLAMA_URL = 'http://localhost:11434'


class HumanReply(BaseModel):
    text: str


async def humanize(original_message: str, result: Dict[str, Any], cfg: Dict[str, Any]) -> str:
    model = ChatOllama(
        model=cfg['model'], base_url=OLLAMA_URL, temperature=cfg['temperature'],
    ).with_structured_output(HumanReply)
    reply = await model.ainvoke(
        f'The user said: "{original_message}"\n\n'
        f'Here is the structured result from handling their request: {result}\n\n'
        'Write a short, natural WhatsApp reply based only on what is in this '
        'result - do not invent anything not present in it.'
    )
    return reply.text
