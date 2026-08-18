"""Loads LangChain-compatible tools from local MCP servers.

Two entirely separate pools, never merged:

- load_mcp_tools() - the CLIENT-FACING pool (search, fetch, crawl). Bound into
  LLMAgent's reasoning cycle, its plain fallback path, and the job composer -
  everything a client's coaching conversation or a scheduled job can reach.
- load_owner_mcp_tools() - the OWNER-ONLY pool (Gmail, Calendar). Bound only into
  services/owner_admin_handler.py's admin agent, which only the platform owner's
  own WhatsApp self-chat can reach. This separation is load-bearing, not
  incidental: a client's coaching message must never be able to trigger a tool
  call that reads the owner's personal email or calendar. If a new owner-only
  integration is ever added, it goes in load_owner_mcp_tools(), never the shared
  pool - see the module docstring on OwnerAdminHandler for the other half of this
  boundary (how the two pools are kept from ever being passed to the same agent).

Search/fetch capability comes from the duckduckgo-mcp-server binary (stdio transport,
no API key required) rather than any Adiyan-specific scraping code - the MCP server
owns that logic entirely. JS-rendered page fetching and multi-page crawling comes
from mcp_servers/crawl4ai_server.py, a thin in-repo wrapper (Crawl4AI has no
official MCP server of its own) run the same way, over stdio. Gmail/Calendar come
from the workspace-mcp package (github.com/taylorwilsdon/google_workspace_mcp) -
UNLIKE every other server here, this one is NOT spawned fresh over stdio per call.
It's a persistent HTTP server (services/workspace_mcp_service.py, main.py starts
it once at startup) that load_owner_mcp_tools() connects to over streamable_http
instead. That's a deliberate exception, not an inconsistency: Google's OAuth
consent flow redirects back some indeterminate time (seconds to minutes, paced by
the owner) after the auth link is generated, and a stdio server's process - along
with the local OAuth callback listener it hosts - dies the moment the tool call
that spawned it returns, almost always before that redirect can land. Confirmed
live. Credentials come from config/secrets_vault.py (the OS Keychain), set once
via tools/set_secret.py, never a plaintext file.

Every server is independently optional within its own pool - one being missing or
misconfigured doesn't stop the others' tools from loading, matching the
platform-wide rule that a local LLM tool being unavailable degrades a turn's
capability, never fails it outright.
"""
import importlib.util
import logging
import shutil
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

logger = logging.getLogger('MCPTools')

# Live-only, never persisted: name -> {'status', 'error', 'tool_count', 'checked_at'}.
# Updated every time _load_one_server runs (startup AND every reload poll) - see
# services/mcp_reload_poller.py. Read by owner_admin_handler.py's list_mcp_servers
# tool and ui/control_panel_api.py's dashboard endpoint, so "why isn't X working"
# gets the real exception, not a shrug. Deliberately not written back into
# mcp_servers.json - see that file's own note on why status must stay out of it.
_server_status: Dict[str, dict] = {}


def get_mcp_server_status() -> Dict[str, dict]:
    return dict(_server_status)

DUCKDUCKGO_MCP_COMMAND = "duckduckgo-mcp-server"
CRAWL4AI_SERVER_SCRIPT = Path(__file__).resolve().parent.parent / 'mcp_servers' / 'crawl4ai_server.py'

# The packaged (PyInstaller --onefile) build freezes Adiyan's own pure-Python
# dependencies into the executable, but duckduckgo-mcp-server, workspace-mcp, and
# crawl4ai are separate command-line tools (crawl4ai additionally needs a one-time
# browser download) - none of that can be frozen into the same single-file binary.
# installer/install.sh installs all three into this standalone venv instead
# (mirroring the existing self-contained openwa/node-runtime/qdrant-runtime
# pattern) and installer/launch_adiyan.sh puts its bin/ on PATH before starting
# Adiyan, so shutil.which() below finds them exactly as it would in a normal dev
# checkout. crawl4ai's own server script has to be resolved from there too when
# frozen: sys.executable in a frozen process is the adiyan binary itself, not a
# python interpreter, so it can't run mcp_servers/crawl4ai_server.py directly.
TOOLS_VENV_DIR = Path.home() / '.Adiyan' / 'app' / 'tools_venv'


def _crawl4ai_server_python_and_script():
    """Returns (python_executable, script_path) for spawning crawl4ai's MCP
    server, or (None, None) if unavailable in this run mode. Dev/source runs use
    the current interpreter and the in-repo script; a frozen build uses the
    installed tools venv on both counts - see the module-level comment above."""
    if getattr(sys, 'frozen', False):
        python_exe = TOOLS_VENV_DIR / 'bin' / 'python3'
        script = TOOLS_VENV_DIR / 'crawl4ai_server.py'
        if python_exe.exists() and script.exists():
            return str(python_exe), str(script)
        return None, None
    if importlib.util.find_spec("crawl4ai") is not None and CRAWL4AI_SERVER_SCRIPT.exists():
        return sys.executable, str(CRAWL4AI_SERVER_SCRIPT)
    return None, None

GOOGLE_CLIENT_ID_KEY = 'GOOGLE_OAUTH_CLIENT_ID'
GOOGLE_CLIENT_SECRET_KEY = 'GOOGLE_OAUTH_CLIENT_SECRET'
GOOGLE_OWNER_EMAIL_KEY = 'GOOGLE_OWNER_EMAIL'


def is_google_workspace_configured() -> bool:
    """Whether Google OAuth credentials have been stored in the vault yet - the
    one status check services/owner_admin_handler.py's check_google_workspace_status
    tool and ui/control_panel_api.py's /api/google-workspace/status endpoint both
    call, so "configured" means the same thing everywhere."""
    from config.secrets_vault import get_secret
    return bool(get_secret(GOOGLE_CLIENT_ID_KEY) and get_secret(GOOGLE_CLIENT_SECRET_KEY))


def _build_server_configs() -> Dict[str, dict]:
    servers = {}

    duckduckgo_command = shutil.which(DUCKDUCKGO_MCP_COMMAND)
    if duckduckgo_command:
        servers["duckduckgo"] = {"command": duckduckgo_command, "args": [], "transport": "stdio"}
    else:
        logger.warning(f"⚠️  {DUCKDUCKGO_MCP_COMMAND} not found on PATH - search/fetch tools unavailable")

    python_exe, script = _crawl4ai_server_python_and_script()
    if python_exe and script:
        servers["crawl4ai"] = {
            "command": python_exe,
            "args": [script],
            "transport": "stdio",
        }
    else:
        logger.warning("⚠️  crawl4ai not installed - JS-rendered fetch/crawl tools unavailable")

    return servers


def _registered_server_config(record: dict) -> dict:
    """Converts one services/mcp_registry.py record into the shape
    MultiServerMCPClient expects. Env var NAMES on the record are resolved to
    real values from the vault here, at load time - the record itself (and the
    JSON file it lives in) only ever holds key names, never secret values."""
    if record['transport'] == 'stdio':
        env = {}
        if record.get('env_var_names'):
            from config.secrets_vault import get_secret
            for var_name in record['env_var_names']:
                value = get_secret(var_name)
                if value:
                    env[var_name] = value
                else:
                    logger.warning(f"⚠️  '{record['name']}': vault has no value for {var_name}")
        return {
            "command": record['command'], "args": record.get('args') or [],
            "transport": "stdio", **({"env": env} if env else {}),
        }
    return {"url": record['url'], "transport": "streamable_http"}


def _load_registered_server_configs(scope: str) -> Dict[str, dict]:
    """Every server in mcp_servers.json matching `scope`, converted to
    MultiServerMCPClient config. A single malformed record is logged and
    skipped, never allowed to take the others down with it - the same
    per-entry isolation _load_one_server applies to the connection itself."""
    from services import mcp_registry
    try:
        records = mcp_registry.list_servers()
    except ValueError as e:
        logger.error(f"❌ Could not read mcp_servers.json, skipping registered servers this pass: {e}")
        return {}

    configs = {}
    for record in records:
        if record.get('scope') != scope:
            continue
        try:
            configs[record['name']] = _registered_server_config(record)
        except (KeyError, TypeError) as e:
            logger.error(f"❌ Registered server '{record.get('name', '?')}' has a malformed record, skipping: {e}")
    return configs


async def _load_one_server(name: str, config: dict) -> List[BaseTool]:
    """One server, one connection attempt, fully isolated from every other
    server - a failure here only ever affects this one entry's own tools and
    its own status record, never the batch. Records the real exception into
    _server_status so list_mcp_servers/the dashboard can show it verbatim,
    not just "failed"."""
    try:
        client = MultiServerMCPClient({name: config})
        tools = await client.get_tools()
        _server_status[name] = {
            'status': 'verified', 'error': None, 'tool_count': len(tools),
            'checked_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        }
        return tools
    except Exception as e:
        logger.error(f"❌ MCP server '{name}' failed to connect: {e}")
        _server_status[name] = {
            'status': 'failed', 'error': str(e), 'tool_count': 0,
            'checked_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        }
        return []


async def load_mcp_tools() -> List[BaseTool]:
    """Load the shared CLIENT-FACING tool pool: the built-in servers plus any
    mcp_servers.json entry marked scope='client_facing'. Returns [] (not an
    error) if none are available, so Adiyan still runs without tool-calling.
    Never add an owner-only integration (email, calendar, anything reading the
    owner's own personal accounts) to this pool - see the module docstring."""
    servers = {**_build_server_configs(), **_load_registered_server_configs('client_facing')}
    if not servers:
        logger.warning("⚠️  No MCP servers available - LLM will run without tools")
        return []

    all_tools: List[BaseTool] = []
    for name, config in servers.items():
        all_tools.extend(await _load_one_server(name, config))
    logger.info(f"✅ Loaded {len(all_tools)} MCP tool(s) from {list(servers)}: {[t.name for t in all_tools]}")
    return all_tools


def load_google_credentials() -> Optional[Dict[str, str]]:
    """Reads GOOGLE_OAUTH_CLIENT_ID/SECRET from the OS Keychain vault
    (config/secrets_vault.py). Returns None if either isn't set, so the caller
    simply skips starting services/workspace_mcp_service.py until the owner
    finishes Google Cloud Console setup and runs tools/set_secret.py - same
    graceful-absence pattern as every other optional MCP server, just gated on a
    vault entry instead of an installed binary. Called from main.py (to decide
    whether to start the persistent workspace-mcp service) and indirectly here."""
    from config.secrets_vault import get_secret
    client_id = get_secret(GOOGLE_CLIENT_ID_KEY)
    client_secret = get_secret(GOOGLE_CLIENT_SECRET_KEY)
    if not client_id or not client_secret:
        return None
    creds = {GOOGLE_CLIENT_ID_KEY: client_id, GOOGLE_CLIENT_SECRET_KEY: client_secret}
    owner_email = get_secret(GOOGLE_OWNER_EMAIL_KEY)
    if owner_email:
        creds[GOOGLE_OWNER_EMAIL_KEY] = owner_email
    return creds


def _pin_owner_email(tool: BaseTool, owner_email: str) -> BaseTool:
    """Force every workspace-mcp tool call to use the real owner email, regardless
    of what the calling LLM supplies - and strip the field from the schema so it
    can't supply anything at all.

    Confirmed live bug this closes: workspace-mcp's own USER_GOOGLE_EMAIL env var
    only changes the parameter's *default* - it stays a visible, optional field in
    the tool schema, and a small local model kept explicitly filling it in with an
    invented placeholder ("owner@example.com") rather than omitting it. An
    explicit LLM-supplied argument always overrides a schema default, so the env
    var alone never stopped the bad value from reaching Google - it just produced
    an authorization link for the wrong account, which the credential store then
    couldn't match to the real stored token, re-triggering the OAuth flow forever.
    Removing the field from the schema removes the LLM's ability to supply a
    value in the first place; forcing it in the wrapped coroutine is the actual
    enforcement (belt and suspenders, since a stripped-but-still-hallucinated
    kwarg would otherwise raise a TypeError instead of silently misbehaving)."""
    import copy

    schema = tool.args_schema
    if not isinstance(schema, dict) or 'user_google_email' not in schema.get('properties', {}):
        return tool

    original_coroutine = tool.coroutine

    async def _pinned_coroutine(**kwargs):
        kwargs['user_google_email'] = owner_email
        return await original_coroutine(**kwargs)

    patched_schema = copy.deepcopy(schema)
    patched_schema['properties'].pop('user_google_email', None)
    if 'required' in patched_schema:
        patched_schema['required'] = [r for r in patched_schema['required'] if r != 'user_google_email']

    tool.coroutine = _pinned_coroutine
    tool.args_schema = patched_schema
    return tool


async def load_owner_mcp_tools(workspace_mcp_url: Optional[str] = None,
                                owner_email: Optional[str] = None) -> List[BaseTool]:
    """Load the OWNER-ONLY tool pool (currently Gmail + Calendar, read-only).
    Never merge this into load_mcp_tools()'s return value or pass it to anything
    but services/owner_admin_handler.py's admin agent - see the module docstring.

    workspace_mcp_url is services/workspace_mcp_service.py's .url once main.py has
    started it (None if Google credentials aren't configured or the service
    failed to start - either way, this just returns [] rather than erroring, same
    as every other optional MCP pool). Connects over HTTP to that already-running
    server rather than spawning anything itself - see the module docstring for
    why this one pool can't use the stdio-per-call pattern every other server
    here uses.

    Read-only by default (--read-only, set when workspace_mcp_service.py starts
    the server) deliberately: this runs inside an autonomous agent loop the owner
    doesn't approve each action of (unlike Claude's own action-confirmation
    rules), so granting it the ability to send email or create/modify calendar
    events by default would be a standing, unsupervised capability - a
    meaningfully different risk than a one-off request. Loosening this is a
    deliberate future opt-in, not a default.
    """
    all_tools: List[BaseTool] = []

    if not workspace_mcp_url:
        logger.info(
            "ℹ️  Google Workspace service not running - owner Gmail/Calendar tools unavailable "
            "(run tools/set_secret.py GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET, "
            "then restart Adiyan, to enable)"
        )
    else:
        workspace_tools = await _load_one_server(
            "google_workspace", {"transport": "streamable_http", "url": workspace_mcp_url},
        )
        if owner_email:
            workspace_tools = [_pin_owner_email(t, owner_email) for t in workspace_tools]
        all_tools.extend(workspace_tools)

    registered = _load_registered_server_configs('owner_only')
    for name, config in registered.items():
        all_tools.extend(await _load_one_server(name, config))

    logger.info(f"✅ Loaded {len(all_tools)} owner-only MCP tool(s): {[t.name for t in all_tools]}")
    return all_tools
