"""
browser - an MCP server exposing real web browsing (search, read, click,
fill forms - full interactive navigation, not just a one-shot page fetch)
to any agent in the mesh, via browser-use (github.com/browser-use/
browser-use, MIT). Free end to end: browser-use drives its own reasoning
loop through this deployment's existing local Ollama (ChatOllama, same
qwen3:8b-16k tool-calling model mesh/lib/agent_sdk.py already defaults to),
never a paid API, and controls a Chromium Playwright installed into this
project's own .venv.

Deliberately a fresh browser_use.Agent (and therefore a fresh Chromium
process) per browse() call, not one long-running session reused across
calls - browser_use.Agent.run() already closes its own browser session in
a `finally` block, so this falls out for free rather than needing explicit
cleanup here. This is a conscious choice, not an oversight: a persistent
Puppeteer/Chromium session is exactly the failure class chronically
degrading OpenWA (mesh/mcp/whatsapp/ + mesh/tools/rotate_openwa.py) -
silent webhook timeouts hours into an unattended run. A browsing task is
occasional, not per-WhatsApp-message, so the few extra seconds of a cold
Chromium launch per call is the right trade for never accumulating that
same long-run degradation.

use_vision=False - this deployment's default model (qwen3:8b-16k) is
text-only; browser-use falls back to its DOM/accessibility-tree
representation instead of screenshots, the same non-visual approach this
mesh's own tool-calling already relies on elsewhere. A vision-capable local
model (qwen3-vl:8b is already pulled) could be swapped in later if
screenshot grounding turns out to matter for some task class, but nothing
today asks for it.

Run from the repo root as `python -m mesh.mcp.browser.server`.
Requires: pip install browser-use && playwright install chromium
(both already done for this deployment's .venv - see mesh/start_all.sh's
own COMPONENTS entry for this server for the exact invocation).
"""
import asyncio
import logging
from typing import Any, Dict

from browser_use import Agent
from browser_use.llm.ollama.chat import ChatOllama
from mcp.server.fastmcp import Context, FastMCP

from mesh.lib import permissions

SERVER_NAME = 'browser'
HOST = '127.0.0.1'
PORT = 8463

# Same default model mesh/lib/agent_sdk.py's ask() already uses - no reason
# for this to diverge from the rest of the mesh's tool-calling default.
DEFAULT_MODEL = 'qwen3:8b-16k'

# A runaway task (the model looping without making progress) burns real
# wall-clock time on local hardware, one Ollama call per step - bounded
# here rather than left at browser_use's own default of 500.
DEFAULT_MAX_STEPS = 15

# browser_use.Agent's own default llm_timeout is a hardcoded per-model-name
# heuristic (browser_use/agent/service.py's _get_model_timeout) tuned for
# cloud APIs - 75s for anything it doesn't recognize by name, which is
# every local Ollama model including ours. Confirmed live: a heavy,
# JS-dense page (Amazon's search results, 200+ items) grew the DOM
# snapshot handed to qwen3:8b-16k large enough that generation on this
# hardware routinely took longer than that 75s window, so the call got
# killed mid-generation on every step past the first few - not a hang, a
# real local-model-vs-cloud-model timeout mismatch, repeating until the
# task burned through its whole failure budget with nothing to show for
# it. Sized generously for CPU-bound local inference on a large prompt,
# not for a fast cloud API. step_timeout (browser_use's own per-step
# ceiling, covering the LLM call plus the actual browser action) raised to
# match - it would otherwise become the new bottleneck the moment
# LLM_TIMEOUT_SECONDS exceeded its own 180s default.
LLM_TIMEOUT_SECONDS = 300
STEP_TIMEOUT_SECONDS = 360

# browser_use's own default (40000) is sized for a fast cloud model that
# can afford to reason over a large DOM snapshot in one call. Halved here
# for the same reason as the timeouts above - a smaller prompt is both
# faster AND less likely to need the full timeout at all on this hardware,
# attacking the root cause (prompt size) rather than only its symptom
# (how long a slow model gets to chew on it).
MAX_CLICKABLE_ELEMENTS_LENGTH = 20000

logger = logging.getLogger(SERVER_NAME)

mcp = FastMCP(SERVER_NAME, host=HOST, port=PORT)


@mcp.tool()
async def browse(task: str, ctx: Context, max_steps: int = DEFAULT_MAX_STEPS) -> Dict[str, Any]:
    """Carries out a browsing task on the open web - search, read a page,
    navigate a multi-page flow, fill a form - and returns what it found.
    `task` should be a complete, self-contained instruction (e.g. "search
    for the current USD to INR exchange rate and report the number"), not a
    bare topic - the calling agent's own prompt, not this tool, is
    responsible for deciding whether a live web lookup is warranted at all
    (see mesh/analysis/skills/analyze.py's strict_grounding rule: whatever
    comes back here still has to be grounded and cited, same as any other
    tool result).

    Returns `success` (whether browser-use itself judged the task
    complete), `result` (its final answer text, or None if it never
    reached one), `steps` (how many actions it took - a proxy for cost),
    and `urls_visited` (for citing where an answer actually came from,
    same grounding discipline as search_documents' own source tracking)."""
    permissions.enforce_mcp_permission(ctx, 'mcp.browser.browse')
    agent = Agent(
        task=task,
        llm=ChatOllama(model=DEFAULT_MODEL),
        use_vision=False,
        llm_timeout=LLM_TIMEOUT_SECONDS,
        step_timeout=STEP_TIMEOUT_SECONDS,
        max_clickable_elements_length=MAX_CLICKABLE_ELEMENTS_LENGTH,
    )
    try:
        history = await agent.run(max_steps=max_steps)
    except Exception as e:
        logger.error(f'Browsing task failed: {e}')
        return {'success': False, 'result': None, 'steps': 0, 'urls_visited': [], 'error': str(e)}

    return {
        'success': history.is_successful(),
        'result': history.final_result(),
        'steps': history.number_of_steps(),
        'urls_visited': history.urls(),
    }


async def main() -> None:
    await mcp.run_streamable_http_async()


if __name__ == '__main__':
    asyncio.run(main())
