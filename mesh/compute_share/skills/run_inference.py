"""
run_inference's real body - what a peer actually exposes to whoever's
inference this call runs. Reachable by any caller, authenticated or not
(see mesh/compute_share/agent_executor.py's PUBLIC_SKILLS for why a
genuinely different Adiyan install can't hold a token this instance
could verify anyway) - the trust boundary is what this function exposes,
not who's calling it. Receives a fully-built prompt and returns a
completion, nothing else. No document, memory, or conversation-history
access exists on this path at all: there's nothing here to leak even by
mistake, because the function's whole signature is (prompt in, text out).
"""
from typing import Any, Dict, Optional

from langchain_ollama import ChatOllama

from mesh.compute_share.constants import OLLAMA_URL


async def run(prompt: str, model: Optional[str] = None) -> Dict[str, Any]:
    llm = ChatOllama(model=model or 'qwen3:8b-16k', base_url=OLLAMA_URL, temperature=0.7)
    result = await llm.ainvoke(prompt)
    return {'completion': result.content, 'model': model or 'qwen3:8b-16k'}
