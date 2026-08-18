"""
The single source of truth for MCP servers registered after Adiyan's three
built-in ones (duckduckgo, crawl4ai, google_workspace - still hardcoded in
core/mcp_tools.py, unaffected by this module). One JSON file, one set of
CRUD functions - every door that can add/update/remove a server (the admin
WhatsApp chat, the dashboard's own forms, a raw HTTP call) goes through the
exact same add_server()/update_server()/remove_server() here, so there is
never a second implementation to drift out of sync.

Deliberately dumb: nothing here ever opens an MCP connection. A record is
structurally validated (right fields, right shape for its transport) before
it's written, but whether it actually WORKS is core/mcp_tools.py's job, run
only at reload - see that module's _load_one_server. Keeping connection
testing out of this module is what lets services/mcp_reload_poller.py treat
"the file changed" and "a server is live" as two separate, independently
retriable concerns.
"""
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger('MCPRegistry')

MCP_SERVERS_FILE = Path.home() / '.Adiyan' / 'mcp_servers.json'

TRANSPORTS = {'stdio', 'streamable_http'}
SCOPES = {'owner_only', 'client_facing'}

# The three servers core/mcp_tools.py already hardcodes - a registered server
# can never take one of these names, so there's no ambiguity about which
# config (hardcoded vs file-backed) wins.
RESERVED_NAMES = {'duckduckgo', 'crawl4ai', 'google_workspace'}


def _read_raw() -> List[Dict[str, Any]]:
    """Raises ValueError on a corrupt file - callers decide how to degrade
    (services/mcp_reload_poller.py keeps its last-known-good pools rather
    than propagating this; an admin tool/dashboard call surfaces it as a
    real error, since silently swallowing a corrupt file just hides the
    problem from the one person who can fix it)."""
    if not MCP_SERVERS_FILE.exists():
        return []
    try:
        with open(MCP_SERVERS_FILE, 'r') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        raise ValueError(f"mcp_servers.json is corrupt: {e}") from e
    if not isinstance(data, list):
        raise ValueError("mcp_servers.json must contain a JSON array")
    return data


def _write_raw(servers: List[Dict[str, Any]]):
    MCP_SERVERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = MCP_SERVERS_FILE.with_suffix('.json.tmp')
    with open(tmp_path, 'w') as f:
        json.dump(servers, f, indent=2)
    import os
    os.replace(tmp_path, MCP_SERVERS_FILE)


def file_hash() -> Optional[str]:
    """None if the file doesn't exist yet - distinct from any real hash, so a
    reload poller can tell "nothing registered" apart from "hash unchanged"."""
    if not MCP_SERVERS_FILE.exists():
        return None
    return hashlib.sha256(MCP_SERVERS_FILE.read_bytes()).hexdigest()


def _validate_entry(name: str, transport: str, command: Optional[str], args: Optional[List[str]],
                     url: Optional[str], scope: str):
    if not name or not name.strip():
        raise ValueError("name is required")
    if name in RESERVED_NAMES:
        raise ValueError(f"'{name}' is a built-in server name and can't be reused")
    if transport not in TRANSPORTS:
        raise ValueError(f"transport must be one of {sorted(TRANSPORTS)}, got '{transport}'")
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {sorted(SCOPES)}, got '{scope}'")
    if transport == 'stdio':
        if not command or not command.strip():
            raise ValueError("transport='stdio' requires a non-empty command")
        if url:
            raise ValueError("transport='stdio' takes command/args, not url")
    else:
        if not url or not url.strip():
            raise ValueError("transport='streamable_http' requires a non-empty url")
        if command:
            raise ValueError("transport='streamable_http' takes url, not command/args")


def list_servers() -> List[Dict[str, Any]]:
    """Raises ValueError on a corrupt file - see _read_raw's docstring for why
    this doesn't swallow that quietly."""
    return _read_raw()


def get_server(name: str) -> Optional[Dict[str, Any]]:
    for s in _read_raw():
        if s['name'] == name:
            return s
    return None


def add_server(name: str, transport: str, command: Optional[str] = None, args: Optional[List[str]] = None,
               url: Optional[str] = None, env_var_names: Optional[List[str]] = None,
               scope: str = 'owner_only') -> Dict[str, Any]:
    """scope defaults to 'owner_only' - never 'client_facing' unless the
    caller explicitly says so. This is the load-bearing default the whole
    design's safety interlock rests on: widening a server's reach to client
    conversations is a deliberate, separate choice, never an accident of a
    missing parameter."""
    servers = _read_raw()
    if any(s['name'] == name for s in servers):
        raise ValueError(f"A server named '{name}' already exists - use update_mcp_server to change it")
    _validate_entry(name, transport, command, args, url, scope)
    record = {
        'name': name,
        'transport': transport,
        'command': command,
        'args': args or [],
        'url': url,
        'env_var_names': env_var_names or [],
        'scope': scope,
        'added_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
    }
    servers.append(record)
    _write_raw(servers)
    logger.info(f"📇 Registered MCP server '{name}' (transport={transport}, scope={scope})")
    return record


def update_server(name: str, **fields) -> Dict[str, Any]:
    allowed = {'transport', 'command', 'args', 'url', 'env_var_names', 'scope'}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    servers = _read_raw()
    existing = next((s for s in servers if s['name'] == name), None)
    if not existing:
        raise ValueError(f"No server named '{name}' - use add_mcp_server to create it")
    merged = {**existing, **updates}
    _validate_entry(merged['name'], merged['transport'], merged.get('command'), merged.get('args'),
                     merged.get('url'), merged['scope'])
    servers = [merged if s['name'] == name else s for s in servers]
    _write_raw(servers)
    logger.info(f"📇 Updated MCP server '{name}': {list(updates)}")
    return merged


def remove_server(name: str) -> bool:
    servers = _read_raw()
    remaining = [s for s in servers if s['name'] != name]
    if len(remaining) == len(servers):
        return False
    _write_raw(remaining)
    logger.info(f"📇 Removed MCP server '{name}'")
    return True
