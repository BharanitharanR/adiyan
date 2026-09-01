"""
Two-phase tool resolution against the Mongo MCP registry (mcp_registry.py)
- Composite selection (same "name + description, pick the relevant one(s)"
interface at the group level and again at the tool level), Strategy
underneath (the selection mechanism is an LLM call today, swappable
without callers knowing), Chain of Responsibility for the empty-result
retry. See docs/TOOL_RESOLUTION_DESIGN.md for the full design.

This is platform code, not agent-specific - any agent whose ReAct loop
wants delegated access to whatever MCP servers are registered calls
resolve_and_execute() with its own `cfg` (model/temperature/timeout) and
gets back one plain-text observation, the same shape any other tool
returns. It never constructs ToolGroup objects by hand; they're read
straight out of the registry.

One tool call at a time by design - no parallel group/tool resolution.
Candidate groups and, within a group, candidate tools are tried in order,
one at a time, until one gives a non-empty result or the attempt budget
runs out.
"""
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from mesh.lib import config_sdk, mcp_registry
from mesh.lib.agent_sdk import AdiyanAgent
from mesh.lib.config import load_seed_config
from mesh.lib.errors import describe_exception

logger = logging.getLogger('ToolResolution')

# Shared platform prompts, config_sdk-backed under the same pseudo agent_id
# convention mesh/lib/skill_router.py already established for its own two
# shared prompts - "given a question, pick a group/tool" is genuinely the
# same structure regardless of which agent's ReAct loop called
# resolve_and_execute(), so this isn't per-agent either.
_SHARED_AGENT_ID = '_tool_resolution'
_agent = AdiyanAgent(_SHARED_AGENT_ID)
_SEED = load_seed_config(Path(__file__).parent)


def _seeded(key: str) -> Dict[str, Any]:
    return _SEED.get(key, {'value': '', 'description': ''})

# Total tool-call attempts (across groups and, within a group, across
# tools) before giving up and returning a not-found observation - bounds
# the retry chain rather than letting it wander indefinitely. Raised from
# 3 - confirmed live, a real schema-discovery-then-query chain
# (list-databases -> list-collections -> find) is genuinely 3 tool calls
# on its own, with zero room left for a single wrong pick along the way.
MAX_ATTEMPTS = 5


@dataclass
class ToolGroup:
    mcp_id: str      # which server to connect to (shared by multiple groups from the same server)
    group_id: str    # which slice of that server's tools this group is (unique to this group)
    name: str
    description: str
    tools: List[Dict[str, Any]]  # raw metadata: {name, description, schema} - not live BaseTool objects


class _GroupSelection(BaseModel):
    group_names: List[str] = Field(
        default_factory=list,
        description="Names of the tool groups relevant to the question, most relevant first. Empty if none apply.",
    )


class _ToolSelection(BaseModel):
    tool_name: Optional[str] = Field(
        default=None,
        description="The specific tool to call next. Omit this (leave it null) ONLY if 'What's been "
                    "discovered so far' already contains enough to answer directly - in that case set "
                    "`answer` instead.",
    )
    args: Dict[str, Any] = Field(default_factory=dict, description="Arguments for that tool call.")
    answer: Optional[str] = Field(
        default=None,
        description="Set this instead of tool_name when what's already been discovered answers the "
                    "instruction directly - e.g. a prior tool call already returned the exact value asked "
                    "for. Leave null while you still need to call a tool.",
    )


async def load_tool_groups() -> List[ToolGroup]:
    """Every registered MCP server as a ToolGroup, read fresh from Mongo -
    metadata only (name/description/schema), no live MCP connection opened
    here. A live connection is only opened at execution time, for the one
    tool actually being called."""
    groups = []
    for meta in await mcp_registry.get_all_group_metas():
        tools = await mcp_registry.get_tools(meta['mcp_id'], meta['group_id'])
        groups.append(ToolGroup(
            mcp_id=meta['mcp_id'], group_id=meta['group_id'],
            name=meta['name'], description=meta['description'], tools=tools,
        ))
    return groups


async def select_groups(question: str, groups: List[ToolGroup], cfg: Dict[str, Any]) -> List[ToolGroup]:
    """Step 1: sees only name + description per group. Returns the
    candidate groups the model named, most relevant first - groups it
    didn't name are dropped, not just reordered."""
    if not groups:
        return []
    listing = '\n'.join(f'- {g.name}: {g.description}' for g in groups)
    seeded = _seeded('select_groups_prompt_template')
    template = await config_sdk.get_constant(
        _SHARED_AGENT_ID, 'select_groups_prompt_template', seeded['value'], description=seeded['description'],
    )
    try:
        prompt = template.format(question=question, listing=listing)
    except Exception:
        prompt = seeded['value'].format(question=question, listing=listing)
    try:
        selection = await _agent.ask(
            prompt, stage='select_groups', model=cfg['model'], temperature=cfg['temperature'], schema=_GroupSelection,
        )
    except Exception as e:
        logger.warning(f'select_groups failed: {describe_exception(e)}')
        return []
    by_name = {g.name: g for g in groups}
    return [by_name[n] for n in selection.group_names if n in by_name]


def _summarize_schema(schema: Dict[str, Any]) -> str:
    """One level deep, argument names only - required vs optional, no
    nested type definitions. A raw MCP tool schema can run to hundreds of
    lines for one tool (nested vector-search variants, enum lists,
    recursive pipeline shapes) - confirmed live, showing all of that for
    every candidate tool in one prompt buried the two genuinely relevant,
    trivial tools (list-collections: just {database}) under a wall of
    JSON irrelevant to the instruction, and plausibly caused a timeout
    outright. This intentionally drops nested detail - a model choosing
    'find over collection-schema' needs to see that find takes a filter
    at all, not the full shape of every possible filter operator."""
    props = schema.get('properties', {})
    required = [n for n in props if n in schema.get('required', [])]
    optional = [n for n in props if n not in schema.get('required', [])]
    # required/optional as two separate, plainly-labeled lists - not an
    # inline marker glued onto the name itself. Confirmed live: an
    # asterisk suffix ("connectionId*") got copied back verbatim as a
    # literal argument key, not read as a required-flag convention.
    parts = []
    if required:
        parts.append('required: ' + ', '.join(required))
    if optional:
        parts.append('optional: ' + ', '.join(optional))
    return '; '.join(parts) if parts else '(no arguments)'


async def select_tool_call(
    question: str, group: ToolGroup, cfg: Dict[str, Any],
    exclude: Optional[set] = None, discoveries: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Step 2, within one group: sees the real tool names, descriptions,
    and schemas for that group only. `exclude` removes tools already tried
    and found empty, so a retry within the same group asks the model to
    pick a genuinely different one, not repeat itself. `discoveries` is
    plain text from prior tool calls THIS SAME resolution made (e.g. a
    real collection name from list-collections) - without it, a tool like
    `find` that needs a real database/collection name the model was never
    told has no way to get one right except guessing; confirmed live, it
    guessed wrong or refused outright ('none'). Returns either a tool call
    plan (has 'tool_name') or a direct answer (has 'answer'), never both.
    Uses the same `cfg` (model/temperature) the calling agent already
    resolved for its own ReAct loop, rather than a second config_sdk
    lookup under some unrelated agent_id."""
    exclude = exclude or set()
    candidates = [t for t in group.tools if t['name'] not in exclude]
    if not candidates:
        return None
    # Schema included, not just name+description - confirmed live: without
    # it, the model guessed plausible-sounding argument names ('query'
    # instead of the real 'filter', a made-up 'collection' value) that
    # failed MCP-side validation instead of ever getting real data. But
    # the FULL raw schema, also confirmed live, is the bigger problem: a
    # few tools (aggregate, explain, export) carry deeply nested
    # vector-search sub-schemas that have nothing to do with a simple
    # lookup, burying the two trivial, actually-relevant tools
    # (list-collections, list-databases) under a wall of irrelevant JSON -
    # this alone was plausibly why one attempt timed out entirely.
    # _summarize_schema strips it down to just what argument names exist
    # and which are required, one level deep, no nested type definitions.
    listing = '\n'.join(f"- {t['name']}: {t['description']}\n  args: {_summarize_schema(t.get('schema', {}))}" for t in candidates)
    if discoveries:
        discoveries_block = (
            "What's been discovered so far, from earlier tool calls this same lookup already made:\n"
            + '\n'.join(f'- {d}' for d in discoveries) + '\n\n'
        )
        answer_seeded = _seeded('select_tool_call_answer_with_discoveries')
        answer_instruction = await config_sdk.get_constant(
            _SHARED_AGENT_ID, 'select_tool_call_answer_with_discoveries',
            answer_seeded['value'], description=answer_seeded['description'],
        )
    else:
        # No mention of `answer` at all on a cold start - confirmed live:
        # even after being told answer requires a real discovery first,
        # the model kept choosing it anyway on the very first attempt
        # ("these tools don't provide access to X"), never trying a
        # single tool. Removing the option from the prompt itself, not
        # just policing it after the fact, is what actually stopped it.
        discoveries_block = ''
        answer_seeded = _seeded('select_tool_call_answer_cold_start')
        answer_instruction = await config_sdk.get_constant(
            _SHARED_AGENT_ID, 'select_tool_call_answer_cold_start',
            answer_seeded['value'], description=answer_seeded['description'],
        )
    prompt_seeded = _seeded('select_tool_call_prompt_template')
    prompt_template = await config_sdk.get_constant(
        _SHARED_AGENT_ID, 'select_tool_call_prompt_template', prompt_seeded['value'], description=prompt_seeded['description'],
    )
    fmt_kwargs = dict(
        question=question, discoveries_block=discoveries_block,
        group_name=group.name, listing=listing, answer_instruction=answer_instruction,
    )
    try:
        prompt = prompt_template.format(**fmt_kwargs)
    except Exception:
        prompt = prompt_seeded['value'].format(**fmt_kwargs)
    valid_names = {t['name'] for t in candidates}
    # Inline retry, not pushed up as a Chain-of-Responsibility "try the
    # next tool" case - confirmed live: the model sometimes names a tool
    # not in the list at all (hallucinated, not merely a bad pick among
    # real options). Silently returning None here previously looked
    # identical to "no candidates left" to the caller, ending the whole
    # attempt with retry budget still unused.
    for _ in range(2):
        try:
            selection = await _agent.ask(
                prompt, stage='select_tool_call', model=cfg['model'], temperature=cfg['temperature'], schema=_ToolSelection,
            )
        except Exception as e:
            logger.warning(f'select_tool_call failed for group {group.name!r}: {e}')
            return None
        # answer is only legitimate once something has actually been
        # discovered - confirmed live: with an empty discoveries list, the
        # model used this same field to give up immediately ("these tools
        # don't provide access to X"), never attempting a single real
        # tool call. Discoveries is prior GENUINE evidence; "I don't think
        # this is possible" is not evidence, and answering with it here
        # would be exactly the ungrounded-answer failure this whole
        # design exists to prevent.
        if selection.answer and discoveries:
            return {'answer': selection.answer}
        if selection.tool_name in valid_names:
            break
        logger.warning(f'select_tool_call picked unknown tool or premature answer {selection.tool_name!r}, retrying')
    else:
        return None
    return {'mcp_id': group.mcp_id, 'tool_name': selection.tool_name, 'args': selection.args}


def _make_connection_interceptor(backend_connection_string: str, needs_connection_id: set):
    """Ported from Analysis Agent's original _make_connection_interceptor(),
    with one correction found live in this new resolution path: the
    original only skipped auto-connect when request.args already carried a
    connectionId, on the assumption a caller that doesn't have one simply
    omits the key. Here, select_tool_call's args come from an LLM given
    that tool's real schema (which lists connectionId as required) with no
    hint that it's auto-managed - it duly invented a plausible-looking
    value ('default_connection') rather than omitting the key, which
    passed the old check and reached the server as a real, wrong
    connectionId ("does not exist or has expired"). Fixed by always
    overriding connectionId for a tool in needs_connection_id, never
    trusting a caller-supplied value for it - this class of tool's
    connection lifecycle is never something a caller (model or otherwise)
    should be filling in itself."""
    cached_id: Optional[str] = None

    async def interceptor(request, handler):
        nonlocal cached_id
        if request.name not in needs_connection_id:
            return await handler(request)
        if cached_id is None:
            connect_result = await handler(
                request.override(name='connect', args={'connectionString': backend_connection_string})
            )
            cached_id = connect_result.structuredContent['connectionId']
        request = request.override(args={**request.args, 'connectionId': cached_id})
        return await handler(request)

    return interceptor


async def _get_live_tool(mcp_id: str, tool_name: str):
    """Opens a connection to exactly one MCP server and returns exactly one
    bound, callable tool - execution-time only, never during selection.
    Two-pass, same as the original: probe without an interceptor to learn
    which raw tool names actually declare a connectionId field (can't be
    guessed from a call's own args - a caller who never provides one omits
    the key entirely, not an empty value), then build the real client with
    the interceptor wired to this group's own connection_config."""
    from langchain_mcp_adapters.client import MultiServerMCPClient

    conn = await mcp_registry.get_connection_config(mcp_id)
    if conn is None:
        return None
    connections = {mcp_id: {'transport': conn['transport'], 'url': conn['url']}}

    probe_client = MultiServerMCPClient(connections, tool_name_prefix=False)
    probe_tools = await probe_client.get_tools()
    needs_connection_id = {
        t.name for t in probe_tools if 'connectionId' in (t.args_schema or {}).get('properties', {})
    }

    backend_connection_string = conn.get('backend_connection_string')
    if needs_connection_id and not backend_connection_string:
        logger.warning(f'{mcp_id!r}: {tool_name!r} needs a connectionId but no backend_connection_string is registered')
        return next((t for t in probe_tools if t.name == tool_name), None)

    client = MultiServerMCPClient(
        connections, tool_name_prefix=False,
        tool_interceptors=[_make_connection_interceptor(backend_connection_string, needs_connection_id)],
    )
    tools = await client.get_tools()
    return next((t for t in tools if t.name == tool_name), None)


async def execute_tool_call(plan: Dict[str, Any]) -> Optional[str]:
    """Runs one resolved tool call. Returns None (not an empty string) on
    "no result", a raised exception, AND an MCP-protocol-level error
    returned as normal content rather than raised - confirmed live: a bad
    argument name (the model picking a wrong tool and inventing plausible-
    sounding args for it) comes back as ordinary tool output shaped like
    {'type': 'text', 'text': 'MCP error -32602: ...'}, not a Python
    exception, so treating anything non-empty as success let a genuine
    failure read as a real answer instead of triggering the same-group
    retry. 'MCP error' is the literal prefix the protocol's own JSON-RPC
    error responses carry, not a guessed string. A real empty-but-
    successful result ('') stays distinct from failure too - both return
    None here, but for different reasons the caller doesn't need to tell
    apart."""
    tool_obj = await _get_live_tool(plan['mcp_id'], plan['tool_name'])
    if tool_obj is None:
        return None
    try:
        result = await tool_obj.ainvoke(plan['args'])
    except Exception as e:
        logger.warning(f"execute_tool_call failed for {plan['tool_name']!r}: {e}")
        return None
    text = str(result).strip()
    if 'MCP error' in text or 'Found 0 results' in text:
        logger.warning(f"execute_tool_call got no usable result for {plan['tool_name']!r}: {text[:200]}")
        return None
    return text or None


async def resolve_and_execute(question: str, cfg: Dict[str, Any]) -> str:
    """The one function most callers need: load the registry, pick a
    group, pick a tool, run it - retrying with the next candidate tool (in
    the same group, then the next group) up to MAX_ATTEMPTS times before
    giving up. Always returns one plain-text observation, the same shape
    any other ReAct tool returns - never raw JSON, never a tool-call
    object.

    Still one tool call at a time, never parallel - but no longer
    "first non-empty result wins": a successful call is only the final
    answer if the model says so (via `answer`). A schema/listing tool
    (e.g. list-collections) genuinely succeeding is real progress, not an
    answer - confirmed live: returning it as one made "here are 83
    MongoDB doc source ids" read as a real reply to a completely unrelated
    question. Every successful call's result is instead carried forward as
    a `discovery`, so a later attempt in the same lookup can use a real,
    now-known collection name instead of guessing one."""
    groups = await load_tool_groups()
    if not groups:
        return 'No internal database tools are currently registered.'

    candidate_groups = await select_groups(question, groups, cfg)
    if not candidate_groups:
        return 'No registered tool group matches this question.'

    discoveries: List[str] = []
    attempts = 0
    for group in candidate_groups:
        tried: set = set()
        while attempts < MAX_ATTEMPTS:
            selection = await select_tool_call(question, group, cfg, exclude=tried, discoveries=discoveries)
            if selection is None:
                break  # no more untried tools in this group
            if 'answer' in selection:
                return selection['answer']
            attempts += 1
            tried.add(selection['tool_name'])
            result = await execute_tool_call(selection)
            if result is not None:
                discoveries.append(f"{selection['tool_name']} returned: {result}")
        if attempts >= MAX_ATTEMPTS:
            break

    if discoveries:
        # The attempt budget ran out without the model ever saying "this
        # answers it" - best-effort fallback is what was actually learned,
        # not silence, but it's explicitly hedged as unconfirmed rather
        # than stated as the answer.
        return f'Not confirmed as a direct answer, but discovered along the way: {discoveries[-1]}'
    return 'No result from the registered database tools for this question.'
