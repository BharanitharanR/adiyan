"""
apply_vertical_spec's real body - parses an uploaded vertical-spec YAML
(produced externally, in Claude or ChatGPT, by the adiyan-vertical-spec
Agent Skill - see agent_skills/adiyan-vertical-spec/) and writes it as a
new business vertical, then activates it deployment-wide.

DataPart-only, not in SKILLS/EXTRACTION_SCHEMAS - same reasoning as
ingest_document/onboard_mcp_server: this needs real file content no
free-text extraction could reliably reconstruct. Owner-only in practice
without any permissions_config.json entry needed for it specifically - the
owner tier's own 'allow' is already ['*'], and no other tier has anything
matching 'config_agent.*' at all, so simply not adding this skill anywhere
else keeps it owner-only.

Every constant this writes is checked against an explicit allowlist below,
never just "does this key already exist on the platform layer" - host,
port, mcp_servers, every *_url, and every *_prompt_template are ALL real,
already-seeded platform constants too, and none of them are safe for a
business owner's generated spec to set directly (see
agent_skills/adiyan-vertical-spec/references/config-vocabulary.md's own
"not yet supported" section for why each is excluded - mainly that a
*_prompt_template contains required {placeholders} Adiyan's own code fills
in by name, and a malformed rewrite would break that stage outright).

All-or-nothing: every field in the spec is validated before anything is
written. A spec naming one field outside the allowlist fails the whole
apply, rather than silently writing the other nine and dropping that one -
the owner would have no way to know which fields actually landed if this
degraded partially instead.
"""
import base64
import logging
import re
from typing import Any, Dict

import yaml

from mesh.config_agent.skills.update_config import _coerce, _CoerceError
from mesh.lib import config_sdk

logger = logging.getLogger('ApplyVerticalSpec')

# agent_id -> the constants a business-persona spec may set for it. Anything
# else that exists on the platform layer for these agents (host, port,
# mcp_servers, every *_prompt_template, every skill_*_description/examples
# pair) is deliberately excluded - see this module's own docstring, and
# agent_skills/adiyan-vertical-spec/references/config-vocabulary.md, which
# ships the exact same list to whichever LLM drafts the spec in the first
# place, so a well-behaved spec should never even attempt one of these.
_ALLOWED_CONSTANTS = {
    'orchestrator': {'summon_phrase', 'card_description', 'business_persona_context'},
    'analysis': {'strict_grounding', 'business_persona_context'},
}

_VERTICAL_ID_RE = re.compile(r'^[a-z0-9]+(-[a-z0-9]+)*$')


class _SpecError(Exception):
    """Raised for any problem with the spec itself - caught once in run()
    so every validation failure returns the same {'applied': False, ...}
    shape instead of an uncaught exception reaching agent_executor.py."""


def _validate_vertical_id(vertical_id: Any) -> str:
    if not isinstance(vertical_id, str) or not _VERTICAL_ID_RE.match(vertical_id):
        raise _SpecError(
            f'vertical_id must be lowercase letters, numbers, and hyphens only (got {vertical_id!r}).'
        )
    if vertical_id == config_sdk.PLATFORM_VERTICAL:
        raise _SpecError("vertical_id cannot be 'platform' - that name is reserved for Adiyan's own defaults.")
    return vertical_id


async def _validate_and_collect_writes(spec: Dict[str, Any]) -> list:
    """Every (agent_id, key, coerced_value) this spec asks for - raises
    _SpecError on the FIRST problem found, rather than collecting a list of
    errors, since any single bad field means nothing should be written."""
    agents = spec.get('agents')
    if not isinstance(agents, dict) or not agents:
        raise _SpecError("Spec must have a non-empty 'agents' section.")

    writes = []
    for agent_id, agent_spec in agents.items():
        if agent_id not in _ALLOWED_CONSTANTS:
            raise _SpecError(
                f'{agent_id!r} is not a business-configurable agent. '
                f'Only {sorted(_ALLOWED_CONSTANTS)} may be set by a vertical spec.'
            )
        if not isinstance(agent_spec, dict):
            raise _SpecError(f'{agent_id!r} entry must be a mapping.')
        constants = agent_spec.get('constants', {})
        if not isinstance(constants, dict):
            raise _SpecError(f'{agent_id!r}.constants must be a mapping.')

        allowed_keys = _ALLOWED_CONSTANTS[agent_id]
        current = await config_sdk.get_full_config(agent_id)
        current_constants = (current or {}).get('constants', {})

        for key, new_value in constants.items():
            if key not in allowed_keys:
                raise _SpecError(
                    f'{agent_id}.{key!r} is not settable by a vertical spec. '
                    f'Allowed for {agent_id!r}: {sorted(allowed_keys)}.'
                )
            # Type-matched against whatever's CURRENTLY on the platform layer
            # for this key - same _coerce() update_config.py's own WhatsApp
            # free-text path already relies on, so "strict_grounding: false"
            # (YAML bool) and "strict_grounding: \"false\"" (a string, if
            # the spec author's tooling quoted it) both land as a real bool,
            # never a truthy non-empty string.
            current_value = current_constants.get(key, '')
            try:
                coerced = _coerce(current_value, new_value)
            except _CoerceError as e:
                raise _SpecError(f'{agent_id}.{key}: {e.message}.')
            writes.append((agent_id, key, coerced))
    return writes


async def run(content_b64: str, filename: str) -> Dict[str, Any]:
    try:
        raw = base64.b64decode(content_b64)
    except Exception as e:
        return {'applied': False, 'error': f'Could not decode the uploaded file: {e}'}

    try:
        spec = yaml.safe_load(raw)
    except Exception as e:
        return {'applied': False, 'error': f'Not valid YAML: {e}'}

    if not isinstance(spec, dict):
        return {'applied': False, 'error': 'Spec must be a YAML mapping at the top level.'}

    try:
        vertical_id = _validate_vertical_id(spec.get('vertical_id'))
        writes = await _validate_and_collect_writes(spec)
    except _SpecError as e:
        return {'applied': False, 'error': str(e)}

    if not writes:
        return {'applied': False, 'error': 'Spec has no recognized fields to apply.'}

    for agent_id, key, value in writes:
        ok = await config_sdk.set_constant(agent_id, key, value, vertical_id=vertical_id)
        if not ok:
            logger.error(f'Failed writing {agent_id}.{key} for vertical {vertical_id!r} - config store may be unreachable.')
            return {'applied': False, 'error': 'Could not write the configuration - the config store may be unreachable.'}

    activated = await config_sdk.set_active_vertical_id(vertical_id)
    if not activated:
        return {
            'applied': True, 'activated': False, 'vertical_id': vertical_id,
            'written': [f'{a}.{k}' for a, k, _ in writes],
            'error': 'Wrote the configuration but could not activate it - try activating manually.',
        }

    return {
        'applied': True, 'activated': True, 'vertical_id': vertical_id,
        'business_name': spec.get('business_name'),
        'written': [f'{a}.{k}' for a, k, _ in writes],
    }
