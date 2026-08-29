"""
onboard_mcp_server's real body - registers one MCP server into the Mongo
registry (mesh/lib/mcp_registry.py) so any agent's resolve_and_execute
(mesh/lib/tool_resolution.py) can use it, with no restart and no code
change to that agent. DataPart-only, not advertised in SKILLS/
EXTRACTION_SCHEMAS - a url/transport/auth payload isn't something a free-
text WhatsApp instruction should fill in, same reasoning update_stage_config
already follows for its own structured, dashboard-only shape.

Fetches the server's real tool list via MCP Inspector's CLI
(https://modelcontextprotocol.io/docs/2026-07-28/tools/inspector) rather
than hand-rolling a probe client - `--method tools/list --format json` is
the protocol's own reference implementation of exactly this call, so this
skill inherits Inspector's own transport/auth handling instead of
reimplementing it.

The group's name/description is not just the human's raw input verbatim -
MCP's own tools/list response has no server-level summary field (only
per-tool descriptions), so a human's raw note plus the real fetched tool
list are both handed to an LLM call, which produces the final name and
description actually stored in the registry's group_meta document. See
docs/TOOL_RESOLUTION_DESIGN.md.
"""
import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_ollama import ChatOllama
from pydantic import BaseModel, Field

from mesh.config_agent.constants import AGENT_ID
from mesh.lib import config_sdk, mcp_registry
from mesh.lib.config import load_seed_config

logger = logging.getLogger('OnboardMCPServer')

INSPECTOR_TIMEOUT_SECONDS = 30
_SEED = load_seed_config(Path(__file__).parent.parent)


def _seeded(key: str) -> Dict[str, Any]:
    return _SEED.get(key, {'value': '', 'description': ''})


class _GroupCluster(BaseModel):
    group_id: str = Field(description="A short snake_case identifier for this cluster, e.g. 'internal_database'.")
    name: str = Field(description="Same as group_id, human-readable if different.")
    description: str = Field(description="One sentence: what this cluster of tools is for, and when an agent should reach for it.")
    tool_names: List[str] = Field(description="The exact names of every tool in this cluster, from the real tool list given.")


class _ClusteringResult(BaseModel):
    groups: List[_GroupCluster] = Field(
        description="One or more clusters. Every tool from the real list must appear in exactly one cluster's tool_names.",
    )


# `transport`, everywhere else in this module and in the registry, is
# langchain_mcp_adapters' own vocabulary (e.g. "streamable_http") - the
# value tool_resolution._get_live_tool() hands straight to
# MultiServerMCPClient at execution time. Inspector CLI names the same
# wire protocol differently ("http"); this is the one place that
# difference is bridged, so nothing downstream has to know it exists.
_INSPECTOR_TRANSPORT_NAMES = {'streamable_http': 'http', 'stdio': 'stdio'}


def _fetch_tools_via_inspector(url: str, transport: str) -> List[Dict[str, Any]]:
    """Shells out to `npx @modelcontextprotocol/inspector --cli ... --method
    tools/list --format json`. Raises on any failure (bad url, server
    unreachable, non-zero exit) - the caller (run()) turns that into a
    clean error result rather than a partially-registered server."""
    inspector_transport = _INSPECTOR_TRANSPORT_NAMES.get(transport, transport)
    cmd = [
        'npx', '@modelcontextprotocol/inspector', '--cli', url,
        '--transport', inspector_transport, '--method', 'tools/list', '--format', 'json',
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=INSPECTOR_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise RuntimeError(f'Inspector CLI exited {result.returncode}: {result.stderr.strip()}')
    payload = json.loads(result.stdout)
    # Confirmed live: Inspector CLI's --format json wraps the raw JSON-RPC
    # response as-is - {"result": {"tools": [...]}} - not just the
    # unwrapped {"tools": [...]} tools/list's own result shape. Accept
    # either, so a future Inspector version that unwraps this itself
    # doesn't silently break this parse.
    if isinstance(payload, dict) and 'result' in payload:
        payload = payload['result']
    raw_tools = payload.get('tools', []) if isinstance(payload, dict) else payload
    return [
        {'name': t['name'], 'description': t.get('description', ''), 'schema': t.get('inputSchema', {})}
        for t in raw_tools
    ]


async def run(
    mcp_id: str, url: str, transport: str, raw_description: str, auth: Optional[Dict[str, Any]] = None,
    backend_connection_string: Optional[str] = None,
) -> Dict[str, Any]:
    try:
        tools = _fetch_tools_via_inspector(url, transport)
    except Exception as e:
        return {'status': 'error', 'message': f'Could not fetch tools from {url!r}: {e}'}

    # connect/disconnect are session-lifecycle tools, not data tools - the
    # resolution step should never be offered them as an answer to a real
    # question. tool_resolution.py manages connectionId automatically, the
    # same reasoning Analysis Agent's original interceptor was built on.
    tools = [t for t in tools if not (t['name'].endswith('connect') or t['name'].endswith('disconnect'))]
    if not tools:
        return {'status': 'error', 'message': f'{url!r} reported zero (non-lifecycle) tools - nothing to register.'}

    cfg = await config_sdk.get_stage_config(
        AGENT_ID, 'describe_group', {'model': 'qwen3:8b-16k', 'temperature': 0.3, 'timeout': 60},
    )
    model = ChatOllama(
        model=cfg['model'], base_url=cfg.get('base_url', 'http://localhost:11434'), temperature=cfg['temperature'],
    ).with_structured_output(_ClusteringResult)
    listing = '\n'.join(f"- {t['name']}: {t['description']}" for t in tools)
    seeded = _seeded('onboard_cluster_prompt_template')
    template = await config_sdk.get_constant(
        AGENT_ID, 'onboard_cluster_prompt_template', seeded['value'], description=seeded['description'],
    )
    try:
        prompt = template.format(raw_description=raw_description, listing=listing)
    except Exception:
        prompt = seeded['value'].format(raw_description=raw_description, listing=listing)
    try:
        clustered = await model.ainvoke(prompt)
    except Exception as e:
        logger.warning(f'describe_group LLM call failed, falling back to one group: {e}')
        clustered = _ClusteringResult(groups=[_GroupCluster(
            group_id=mcp_id, name=mcp_id, description=raw_description, tool_names=[t['name'] for t in tools],
        )])

    tools_by_name = {t['name']: t for t in tools}
    groups = []
    assigned = set()
    for cluster in clustered.groups:
        cluster_tools = [tools_by_name[n] for n in cluster.tool_names if n in tools_by_name]
        if not cluster_tools:
            continue
        assigned.update(t['name'] for t in cluster_tools)
        groups.append({
            'group_id': cluster.group_id, 'name': cluster.name,
            'description': cluster.description, 'tools': cluster_tools,
        })
    # Any tool the clustering step didn't place (a name it didn't repeat
    # exactly, or genuinely skipped) still gets registered, not silently
    # dropped - into its own catch-all rather than merged into a cluster
    # whose stated purpose it might not actually match.
    leftover = [t for t in tools if t['name'] not in assigned]
    if leftover:
        groups.append({
            'group_id': f'{mcp_id}_other', 'name': f'{mcp_id}_other',
            'description': f'Other tools from {mcp_id!r} not otherwise categorized.',
            'tools': leftover,
        })

    await mcp_registry.upsert_connection_config(mcp_id, url, transport, auth, backend_connection_string)
    await mcp_registry.replace_groups(mcp_id, groups)

    return {
        'status': 'registered',
        'mcp_id': mcp_id,
        'groups': [{'group_id': g['group_id'], 'name': g['name'], 'description': g['description'], 'tool_count': len(g['tools'])} for g in groups],
    }
