"""
Generic MCP tool-call helper, shared by any agent under mesh/ that's a client
of an MCP server (e.g. Scheduler Agent calling cron_trigger's
register_trigger). Deliberately not routed through an LLM/LangChain tool-call
decision - a call like "register this exact job_id at this exact datetime"
is already fully determined by the caller's own code, not something that
needs a model to choose to invoke. Reserve LangChain-mediated tool calls for
cases where an LLM is actually the one deciding whether/how to call a tool.

Uses the mcp package's low-level client (streamablehttp_client + ClientSession)
- not the newer mcp.Client class, which doesn't exist in the 1.x line this
repo has installed (requirements.txt pins mcp>=1.20.0; verified 1.26.0 is
actually installed). Every MCP server under mesh/mcp/ speaks streamable-http,
not stdio, for the same persistent-process reason documented in
mesh/mcp/cron_trigger/server.py.

verify=False on the underlying httpx client: some MCP servers (whatsapp)
serve self-signed HTTPS (mesh/lib/tls.py) - these are all our own local
services calling each other, not a browser trusting an arbitrary site, so
skipping CA verification here is the same trust model already accepted via
`curl -k` while testing. Harmless no-op for plain-HTTP targets (cron_trigger).
"""
from typing import Any, Dict, Optional

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared._httpx_utils import MCP_DEFAULT_SSE_READ_TIMEOUT, MCP_DEFAULT_TIMEOUT


def _insecure_http_client_factory(headers=None, timeout=None, auth=None) -> httpx.AsyncClient:
    kwargs: Dict[str, Any] = {'follow_redirects': True, 'verify': False}
    kwargs['timeout'] = timeout or httpx.Timeout(MCP_DEFAULT_TIMEOUT, read=MCP_DEFAULT_SSE_READ_TIMEOUT)
    if headers is not None:
        kwargs['headers'] = headers
    if auth is not None:
        kwargs['auth'] = auth
    return httpx.AsyncClient(**kwargs)


async def call_tool(
    server_url: str, tool_name: str, arguments: Dict[str, Any], token: Optional[str] = None,
) -> Any:
    """Opens a fresh session, calls one tool, closes. No connection pooling -
    these calls are infrequent (e.g. once per scheduled job created), not
    worth keeping a persistent client session open for.

    token: a permission token (mesh/lib/permissions.py), sent as a plain
    Authorization header - confirmed the real Starlette Request (headers
    included) survives to a tool function's ctx.request_context.request on
    the receiving MCP server, via streamable_http.py's own
    ServerMessageMetadata(request_context=request)."""
    headers = {'Authorization': f'Bearer {token}'} if token else None
    async with streamablehttp_client(
        server_url, headers=headers, httpx_client_factory=_insecure_http_client_factory,
    ) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments)
            if result.isError:
                raise RuntimeError(f'{tool_name} failed: {result.content}')
            content = result.structuredContent
            # Confirmed at the SDK source (func_metadata.py): a tool whose
            # return type is a plain dict (every @mcp.tool() in this repo is
            # typed -> Dict[str, Any], not a named Pydantic model) always
            # gets wrapped as {"result": <value>} for its structured-content
            # schema - "Generic types (list, dict, Union, etc.) - wrapped in
            # a model with a 'result' field." Every caller here wants the
            # tool's own logical return value, not this SDK envelope -
            # confirmed live: get_own_phone's real {'phone': ...} arrived as
            # {'result': {'phone': ...}}, silently breaking a plain
            # content.get('phone') read.
            if isinstance(content, dict) and set(content.keys()) == {'result'}:
                return content['result']
            return content
