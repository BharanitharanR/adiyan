"""
Classifies a model name into a coarse capability tier ('small' or 'large') -
a plain parameter-count parse, no LLM call, no network I/O. Exists because
of a real, observed failure this session: the exact same decide_next_step
prompt in mesh/analysis/skills/analyze.py reliably confused qwen3:4b
(looping on pointless tool calls, or replying with neither a tool call nor
real content) while qwen3:8b-16k handled the identical prompt correctly on
the first try - see ~/.Adiyan/logs/llm_calls.log for the actual side-by-
side evidence, not a hypothetical. Smaller models often need more explicit,
more constrained prompting to reliably follow a multi-step instruction;
a larger one can work from terser phrasing that would otherwise leave a
smaller model guessing.

Only two tiers today, not because more couldn't exist, but because that's
all any prompt actually needs differentiated so far - see
config_sdk.get_tiered_constant() for how a prompt keys off this.
"""
import re
from typing import Optional

from mesh.lib import config_sdk

_TIER_AGENT_ID = '_model_tiers'
DEFAULT_SMALL_MODEL_MAX_PARAMS_B = 6.0

_PARAM_COUNT_RE = re.compile(r'(\d+(?:\.\d+)?)b', re.IGNORECASE)


def _parse_param_count_b(model_name: str) -> Optional[float]:
    """Billions of parameters parsed from a model name like 'qwen3:4b' or
    'qwen3:8b-16k' - the digits immediately before a 'b', case-insensitive.
    None if the name doesn't follow this convention (e.g. a bare alias with
    no size in it) - callers treat that as 'large' rather than guessing,
    since wrongly assuming a model needs the more constrained small-model
    prompt is the worse failure direction than the reverse."""
    match = _PARAM_COUNT_RE.search(model_name)
    return float(match.group(1)) if match else None


async def model_tier(model_name: str) -> str:
    """'small' or 'large'. The threshold is a config_sdk constant, not a
    hardcoded number - dashboard-tunable if a future model's real behavior
    doesn't match what its nominal parameter count would suggest."""
    threshold = await config_sdk.get_constant(
        _TIER_AGENT_ID, 'small_model_max_params_b', DEFAULT_SMALL_MODEL_MAX_PARAMS_B,
        description=(
            'Models at or below this parameter count (billions) get the small-model '
            'prompt variant where one exists, e.g. 4 for qwen3:4b. Parsed from the '
            'model name (the digits before "b") - a name with no parseable size is '
            'always treated as large.'
        ),
    )
    params_b = _parse_param_count_b(model_name)
    if params_b is None:
        return 'large'
    return 'small' if params_b <= threshold else 'large'
