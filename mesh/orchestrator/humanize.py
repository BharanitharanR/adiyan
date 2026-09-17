"""
Turns a target agent's raw structured result into a short, natural reply.
Ported from mesh/whatsapp_connector/humanize.py (now retired) - same logic.
Every mesh/ agent returns Part.data (structured JSON) meant for a machine
caller; this is where that gets turned into words for a human, grounded
only in what the result dict actually contains.

The prompt template itself is Mongo-backed via config_sdk, seeded from
mesh/orchestrator/seed_config.json (not a hardcoded Python literal here) -
see mesh/lib/config_sdk.py's seed_from_file() and
mesh/analysis/skills/analyze.py's own _seeded() for the same pattern
applied agent-wide. The seed file's value is only the first-seed value and
the fallback if Mongo is unreachable or the on-file template is malformed,
never the value actually used once Mongo has one on file.

POC: routed through mesh/lib/agent_sdk.py's ask() (AdiyanAgent), not a
direct ChatOllama call - the first real caller of ask() anywhere in this
mesh (confirmed live: grepping for `.ask(` across every agent turned up
none before this). That's what makes `community` (see run()'s own
docstring, handle_message.py's trigger-word detection) actually mean
something for this call: an offload-eligible completion Inference Router
can hand to a peer when this machine is backed up (or, per complete.py's
own fallback, when Ollama itself isn't even reachable) rather than one of
the many ChatOllama call sites still bypassing this entirely mesh-wide.

Real, deliberate cost of this migration: this used to be
.with_structured_output(HumanReply), which ask() itself documents as
"always local, never offloadable, regardless of `community`" - a raw
pydantic model can't cross an A2A call to a peer. Structured output also
happened to guarantee just the reply text, no model preamble ("Sure,
here's a friendly reply:") ever reaching WhatsApp. Trading that guarantee
for offload-eligibility is exactly the POC tradeoff asked for here, not
an oversight - the prompt below explicitly instructs plain output instead
of relying on the schema to enforce it, but a model that ignores that
instruction can now leak preamble into a real reply where it couldn't
before.
"""
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from mesh.lib import config_sdk
from mesh.lib.agent_sdk import AdiyanAgent
from mesh.lib.config import load_seed_config
from mesh.orchestrator.constants import AGENT_ID

logger = logging.getLogger('Humanize')

_SEED = load_seed_config(Path(__file__).parent)

_agent = AdiyanAgent(AGENT_ID)


def _seeded(key: str) -> Dict[str, Any]:
    return _SEED.get(key, {'value': '', 'description': ''})


async def humanize(
    original_message: str, result: Dict[str, Any], cfg: Dict[str, Any], community: Optional[str] = None,
    language: Optional[str] = None, is_owner: bool = False, vertical_id: Optional[str] = None,
) -> str:
    """language: when given, the reply is composed directly in that
    language instead of whatever language `original_message` happens to
    be in - built for a transcribed voice note (mesh/orchestrator/skills/
    handle_message.py's own audio handling), where the underlying skill's
    raw result is grounded in English documents/tools but the sender
    actually asked in Tamil. qwen3:8b-16k (this stage's own model) already
    has real multilingual ability and is properly chat-tuned - simpler and
    more reliable than routing an Indic-specific raw-completion model
    (sarvam-1) into this step, which was tried and confirmed live this
    session to need awkward few-shot/stop-sequence handling this ordinary
    chat call doesn't.

    is_owner: True only for the deployment's own owner (every caller must
    pass this explicitly - it does not default to safe-for-customers or
    safe-for-owner, since either wrong default is wrong for someone).
    Forces the platform layer (config_sdk.PLATFORM_VERTICAL), bypassing
    whatever vertical_id was passed in, so the owner always sees Adiyan's
    own voice - never their own customer-facing persona. See analyze.py's
    run() for the same branch applied to reasoning rather than
    reply-wording.

    vertical_id: which vertical's own summon phrase this message matched
    (rules_engine.check(), threaded through handle_message.py's own call
    sites) - None falls back to config_sdk's legacy deployment-wide
    "active" resolution, same as omitting it always has."""
    effective_vertical_id = config_sdk.PLATFORM_VERTICAL if is_owner else vertical_id

    seeded = _seeded('humanize_prompt_template')
    template = await config_sdk.get_constant(
        AGENT_ID, 'humanize_prompt_template', seeded['value'], vertical_id=effective_vertical_id, description=seeded['description'],
    )
    try:
        prompt = template.format(original_message=original_message, result=result)
    except Exception as e:
        # A template edited (via the WhatsApp/dashboard tool) into
        # something that doesn't actually have {original_message}/{result}
        # placeholders must not break every reply in the mesh - fall back
        # to the known-good seed default rather than erroring out.
        logger.warning(f'humanize_prompt_template on file is malformed, using seed default: {e}')
        prompt = seeded['value'].format(original_message=original_message, result=result)

    # Business persona is no longer fetched here - mesh/lib/persona_hook.py
    # (read automatically inside _agent.ask() below, via
    # mesh/lib/bootstrap.py's executor wrapper) handles this platform-wide
    # now, for every agent, not just this one. is_owner is still needed
    # above though, for the vertical_id bypass on humanize_prompt_template
    # itself - that part is genuinely this function's own, not something
    # persona_hook generalizes.

    # No schema now (see module docstring) - the model has to be told in
    # plain words to skip preamble, since with_structured_output isn't here
    # anymore to enforce that shape for us.
    prompt += (
        "\n\nRespond with ONLY the reply text itself - no preamble, no "
        "explanation of what you're doing, no quotes around it."
    )
    if language:
        prompt += f"\n\nWrite the reply in {language}, regardless of what language the result above is in."
    return await _agent.ask(prompt, stage='humanize', model=cfg['model'], temperature=cfg['temperature'], community=community)
