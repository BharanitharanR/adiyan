"""
analyze_document's real body - the actual map-reduce analysis pipeline.
Fetches an entire document's text from Memory Agent (not a handful of
similarity-matched snippets - mesh/memory/skills/search_kb.py's job is
"answer one fact," this is "read everything and reason about it"), splits
it into windows sized to the analysis model's own context budget (NOT the
much smaller ~800-char chunks memory_index.py stores for embedding
precision - reusing those would mean dozens of needlessly small LLM calls
for one document), runs the caller's instruction against each window, then
synthesizes the partial findings into one final answer.

Document resolution ("which document does 'the AI native payment
validation presentation' mean") is Memory Agent's own job
(resolve_document, wrapping its existing find_source_document) - this
module only calls it when source_filename isn't already known (the
free-text follow-up case); the upload+instruct combined flow
(mesh/orchestrator/skills/handle_message.py) already knows exactly which
document it means and skips resolution entirely.

No map-reduce size ceiling here - an arbitrarily long document just means
more windows and more map calls, not a silent truncation. The one real
limit is wall-clock/cost, which is exactly why this whole skill is
owner-only (mesh/lib/permissions_config.json).
"""
import base64
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_ollama import ChatOllama
from llama_index.core.node_parser import SentenceSplitter
from pydantic import BaseModel

from mesh.analysis.constants import MEMORY_AGENT_URL
from mesh.lib import permissions
from mesh.lib.a2a_client import call_agent
from mesh.lib.config import load_runtime_config

AGENT_CODE_DIR = Path(__file__).parent.parent
logger = logging.getLogger('AnalyzeDocument')

# Sized for qwen3's 16k-token context, not memory_index.py's ~800-char
# embedding chunks - ~4 chars/token is a conservative rough estimate, and
# this leaves headroom for the instruction text and the model's own output
# within the same context window.
WINDOW_CHARS = 8000
WINDOW_OVERLAP = 200

# Past this length, the synthesized result is delivered as a file instead
# of a WhatsApp text message - see handle_message.py's content_b64
# delivery convention (any skill result carrying content_b64 is understood
# as "deliver a file," not specific to this skill_id).
FILE_DELIVERY_THRESHOLD_CHARS = 1500

_splitter = SentenceSplitter(chunk_size=WINDOW_CHARS, chunk_overlap=WINDOW_OVERLAP)


class _PartialFinding(BaseModel):
    finding: str


class _SynthesizedResult(BaseModel):
    result: str


async def _resolve_document_text(instruction: str, source_filename: Optional[str]) -> Dict[str, Any]:
    """{'found', 'source_filename', 'text'}. Two Memory Agent calls when
    source_filename isn't already known (fuzzy-resolve, then fetch); one
    when it is (fetch only). A service token, not the original caller's
    own - same convention mesh/journal/skills/craft_reflection_prompt.py
    already uses for its own call into Memory Agent: this is an internal
    agent-to-agent call, not something the original caller directly asked
    Memory Agent for."""
    token = permissions.mint_token('analysis', 'service')

    if source_filename is None:
        resolved = await call_agent(MEMORY_AGENT_URL, 'resolve_document', {'query': instruction}, token=token)
        if not resolved.get('found'):
            return {'found': False, 'source_filename': None, 'text': None}
        source_filename = resolved['source_filename']

    fetched = await call_agent(
        MEMORY_AGENT_URL, 'get_document_text', {'source_filename': source_filename}, token=token,
    )
    if not fetched.get('found'):
        return {'found': False, 'source_filename': source_filename, 'text': None}
    return {'found': True, 'source_filename': source_filename, 'text': fetched['text']}


async def _analyze_window(
    instruction: str, window: str, window_index: int, total_windows: int, cfg: Dict[str, Any],
) -> str:
    model = ChatOllama(
        model=cfg['model'], base_url=cfg.get('base_url', 'http://localhost:11434'), temperature=cfg['temperature'],
    ).with_structured_output(_PartialFinding)
    result = await model.ainvoke(
        f'You are analyzing part {window_index + 1} of {total_windows} of a document.\n'
        f'Instruction: {instruction}\n\n'
        'Report only what you actually find in THIS excerpt - if there is nothing '
        'relevant here, say so plainly rather than inventing something.\n\n'
        f'--- Excerpt ---\n{window}'
    )
    return result.finding


async def _synthesize(instruction: str, partial_findings: List[str], cfg: Dict[str, Any]) -> str:
    model = ChatOllama(
        model=cfg['model'], base_url=cfg.get('base_url', 'http://localhost:11434'), temperature=cfg['temperature'],
    ).with_structured_output(_SynthesizedResult)
    findings_block = '\n\n'.join(f'Part {i + 1}: {f}' for i, f in enumerate(partial_findings))
    result = await model.ainvoke(
        f'Instruction: {instruction}\n\n'
        'Below are findings from each part of a document, analyzed separately. '
        'Combine them into one coherent final answer - do not just concatenate '
        'them verbatim, and do not invent anything not present in them. If every '
        'part found nothing relevant, say so plainly.\n\n'
        f'{findings_block}'
    )
    return result.result


def _package_result(text: str, source_filename: str) -> Dict[str, Any]:
    if len(text) <= FILE_DELIVERY_THRESHOLD_CHARS:
        return {'found': True, 'result': text}
    base_name = source_filename.rsplit('/', 1)[-1].rsplit('.', 1)[0]
    return {
        'found': True,
        'result': text[:200] + '...' if len(text) > 200 else text,
        'filename': f'{base_name}_analysis.md',
        'mimetype': 'text/markdown',
        'content_b64': base64.b64encode(text.encode('utf-8')).decode('ascii'),
    }


async def run(instruction: str, source_filename: Optional[str] = None) -> Dict[str, Any]:
    cfg = load_runtime_config(AGENT_CODE_DIR)

    resolved = await _resolve_document_text(instruction, source_filename)
    if not resolved['found']:
        return {'found': False, 'result': "I couldn't find a matching document in the knowledge base."}

    windows = _splitter.split_text(resolved['text'])
    findings = [
        await _analyze_window(instruction, window, i, len(windows), cfg['analyze_window'])
        for i, window in enumerate(windows)
    ]
    synthesized = findings[0] if len(findings) == 1 else await _synthesize(instruction, findings, cfg['synthesize'])

    return _package_result(synthesized, resolved['source_filename'])
