"""
Runs one LLM call through the Claude CLI (`claude -p`) - the alternative
backend AdiyanAgent.ask() can pick for plain-text and schema calls instead of
local Ollama. See agent_sdk.py's ask() for when it's chosen, and
mesh/lib/privacy_mode.py for the per-contact switch that forces it off.

The flag set is what keeps a call cheap: a bare `claude -p` loads Claude
Code's full built-in prompt and tool list (~44k input tokens, ~$0.09 for a
one-line classify, measured). A short system prompt, no tools, no settings,
no skills and no MCP servers, run from an empty working directory so no
CLAUDE.md is picked up, brought the same call to ~1.3k tokens, ~$0.0025.

Never raises anything but ClaudeCliError - the caller falls back to Ollama on
any failure (binary missing, not logged in, rate limited, timeout, bad
output), so the CLI being unavailable degrades a call, never breaks it.
"""
import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import Any, Dict, Optional, Type

from pydantic import BaseModel

logger = logging.getLogger('ClaudeCLI')

SYSTEM_PROMPT = 'You are a precise component inside a larger system. Follow the instructions in the user message exactly.'

# Empty on purpose - a CLAUDE.md or project settings found in the working
# directory would be loaded into every call.
_CWD = Path.home() / '.Adiyan' / 'claude_cli_cwd'

_semaphore: Optional[asyncio.Semaphore] = None
_semaphore_size = 0


class ClaudeCliError(RuntimeError):
    pass


def _binary() -> str:
    found = shutil.which('claude')
    if found:
        return found
    fallback = Path.home() / '.local' / 'bin' / 'claude'
    if fallback.exists():
        return str(fallback)
    raise ClaudeCliError('claude CLI not found on PATH or in ~/.local/bin')


def _limiter(size: int) -> asyncio.Semaphore:
    global _semaphore, _semaphore_size
    if _semaphore is None or _semaphore_size != size:
        _semaphore = asyncio.Semaphore(size)
        _semaphore_size = size
    return _semaphore


async def _execute(command: list, prompt: str, timeout_s: float, max_concurrency: int) -> Dict[str, Any]:
    _CWD.mkdir(parents=True, exist_ok=True)
    async with _limiter(max_concurrency):
        process = await asyncio.create_subprocess_exec(
            *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=str(_CWD),
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(prompt.encode()), timeout=timeout_s)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise ClaudeCliError(f'timed out after {timeout_s}s')

    try:
        data = json.loads(stdout)
    except ValueError:
        raise ClaudeCliError(f'unparseable output: {stdout[:200]!r} stderr={stderr[:200]!r}')
    if data.get('is_error'):
        raise ClaudeCliError(str(data.get('result') or data.get('api_error_status') or 'unknown CLI error'))
    return data


_BASE_FLAGS = [
    '--exclude-dynamic-system-prompt-sections', '--disable-slash-commands',
    '--strict-mcp-config', '--setting-sources', '', '--no-session-persistence', '--output-format', 'json',
]


async def run(
    prompt: str, *, model: str, schema: Optional[Type[BaseModel]] = None, json_schema: Optional[dict] = None,
    timeout_s: float = 90.0, max_concurrency: int = 3,
) -> Dict[str, Any]:
    """The CLI's parsed JSON result. Plain text is in ['result']; with a
    schema, the structured reply is in ['structured_output']. `schema` is a
    Pydantic class; `json_schema` is a ready-made JSON Schema dict (used for
    tool selection, where the shape is built at call time). The prompt goes
    over stdin, not argv - persona + history + memory can run to tens of KB."""
    command = [_binary(), '-p', '--model', model, '--system-prompt', SYSTEM_PROMPT, '--tools', '', *_BASE_FLAGS]
    if schema is not None:
        command += ['--json-schema', json.dumps(schema.model_json_schema())]
    elif json_schema is not None:
        command += ['--json-schema', json.dumps(json_schema)]
    return await _execute(command, prompt, timeout_s, max_concurrency)


WEB_SYSTEM_PROMPT = (
    'You answer web research tasks. Use the web tools to find the facts, then answer concisely. '
    'State only what the pages you read say, and end with the URLs you used. Follow any site '
    'restrictions given in the task (for example, only one named website). If you cannot find '
    'it, say so plainly instead of guessing.'
)


async def web_research(
    task: str, *, model: str, timeout_s: float = 150.0, max_budget_usd: float = 0.30, max_concurrency: int = 3,
) -> Dict[str, Any]:
    """One research task through the CLI's own WebSearch/WebFetch tools - the
    CLI runs its own search-and-read loop, so this is a single process, not a
    loop driven from here. The only call shape with tools enabled; the budget
    cap stops a runaway search from spending more than max_budget_usd."""
    command = [
        _binary(), '-p', '--model', model, '--system-prompt', WEB_SYSTEM_PROMPT,
        '--tools', 'WebSearch,WebFetch', '--allowedTools', 'WebSearch,WebFetch',
        '--max-budget-usd', str(max_budget_usd), *_BASE_FLAGS,
    ]
    return await _execute(command, task, timeout_s, max_concurrency)
