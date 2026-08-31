"""
run_inference's real body - what a peer actually exposes to whoever's
inference this call runs. Deliberately the only thing a 'peer'-tier
caller is allowed to reach (see mesh/lib/permissions_config.json) -
receives a fully-built prompt and returns a completion, nothing else.
No document, memory, or conversation-history access exists on this path
at all: there's nothing here to leak even by mistake, because the
function's whole signature is (prompt in, text out).
"""
from typing import Any, Dict, Optional

from langchain_ollama import ChatOllama

from mesh.compute_share.constants import OLLAMA_URL


async def run(prompt: str, model: Optional[str] = None) -> Dict[str, Any]:
    llm = ChatOllama(model=model or 'qwen3:8b-16k', base_url=OLLAMA_URL, temperature=0.7)
    result = await llm.ainvoke(prompt)
    return {'completion': result.content, 'model': model or 'qwen3:8b-16k'}
