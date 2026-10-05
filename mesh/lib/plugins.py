"""
Adiyan plugins: agents that live outside this repo and are installed into
~/.Adiyan/plugins/<module_root>/ (override with ADIYAN_PLUGINS_DIR), each with
an adiyan-plugin.json manifest. Core reads the manifests instead of carrying
plugin-specific lines:

- mesh/tools/runnable_agents.py registers each plugin's server module, so
  mesh/start_all.sh launches it like any other agent (start_all.sh puts the
  plugins folder on PYTHONPATH);
- mesh/lib/permissions.py adds each plugin's permission tier;
- mesh/nginx/generate_config.py applies each plugin's gateway settings.

A plugin can only ask for what core lets plugins have (GRANTABLE and
GATEWAY_DIRECTIVES below) - never '*', never owner powers, never another
agent's skills, never an existing tier's name. A manifest that asks for more
is rejected whole, not partly applied.

Manifest (adiyan-plugin.json):
{
  "id": "genie",                         # becomes the agent id
  "name": "Daily Practice Genie",
  "version": "1.0.0",
  "module_root": "daily_practice_genie", # the Python package; installed to plugins/<module_root>
  "server_module": "daily_practice_genie.server",
  "port": 8442,
  "tier": "genie_service",               # optional: the permission tier this plugin mints for itself
  "permissions": ["mcp.whatsapp.send_message"],
  "gateway": ["client_max_body_size 20m;"],
  "secrets": [{"name": "GENIE_DEVICE_KEY", "generate": "random40", "description": "..."}],
  "requires": ["voicebox", "whatsapp"],
  "description": "..."
}
"""
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List

PLUGINS_DIR = Path(os.environ.get('ADIYAN_PLUGINS_DIR', str(Path.home() / '.Adiyan' / 'plugins')))
MANIFEST = 'adiyan-plugin.json'

# What a plugin may be granted. Deliberately narrow: messaging through the
# owner's WhatsApp link, and reading which number that link is. Widening this
# is a core decision, made here, not by a manifest.
GRANTABLE = {
    'mcp.whatsapp.send_message',
    'mcp.whatsapp.resolve_chat_id',
    'mcp.whatsapp.get_own_phone',
}

# nginx directives a plugin may set on its own /agents/<id>/ location, with
# the values each accepts.
GATEWAY_DIRECTIVES = {
    'client_max_body_size': re.compile(r'^\d{1,3}[km]$'),
    'client_body_buffer_size': re.compile(r'^\d{1,3}[km]$'),
    'proxy_buffering': re.compile(r'^(on|off)$'),
    'proxy_max_temp_file_size': re.compile(r'^\d{1,4}[km]?$'),
    'proxy_read_timeout': re.compile(r'^\d{1,4}s$'),
    'proxy_send_timeout': re.compile(r'^\d{1,4}s$'),
}

_ID = re.compile(r'^[a-z][a-z0-9_]{1,40}$')
_cache: Dict[str, Any] = {'at': 0.0, 'plugins': []}


class PluginError(ValueError):
    pass


def validate(manifest: Dict[str, Any], core_tiers: List[str] = ()) -> Dict[str, Any]:
    """Returns the manifest if it's acceptable, or raises PluginError saying why."""
    m = manifest
    for key in ('id', 'name', 'version', 'module_root', 'server_module', 'port'):
        if key not in m:
            raise PluginError(f'manifest is missing "{key}"')
    if not _ID.match(str(m['id'])) or not _ID.match(str(m['module_root'])):
        raise PluginError('id and module_root must be lower-case letters, digits and _')
    if not str(m['server_module']).startswith(m['module_root'] + '.'):
        raise PluginError('server_module must be inside module_root')
    if not isinstance(m['port'], int) or not 1024 < m['port'] < 65535:
        raise PluginError('port must be a number between 1025 and 65534')
    perms = m.get('permissions', [])
    bad = [p for p in perms if p not in GRANTABLE]
    if bad:
        raise PluginError(f'plugins may not be granted: {", ".join(bad)}')
    tier = m.get('tier')
    if perms and not tier:
        raise PluginError('a plugin asking for permissions must name its own "tier"')
    if tier and (not _ID.match(tier) or tier in core_tiers):
        raise PluginError(f'tier "{tier}" is not allowed (it must be new and lower-case)')
    for line in m.get('gateway', []):
        parts = str(line).rstrip(';').split()
        if len(parts) != 2 or parts[0] not in GATEWAY_DIRECTIVES or not GATEWAY_DIRECTIVES[parts[0]].match(parts[1]):
            raise PluginError(f'gateway setting not allowed: {line!r}')
    for s in m.get('secrets', []):
        if not re.match(r'^[A-Z][A-Z0-9_]{2,60}$', str(s.get('name', ''))):
            raise PluginError(f'bad secret name: {s.get("name")!r}')
    return m


def _core_tier_names() -> List[str]:
    path = Path(__file__).parent / 'permissions_config.json'
    try:
        return list(json.loads(path.read_text())['tiers'])
    except Exception:
        return []


def installed() -> List[Dict[str, Any]]:
    """Every valid, enabled plugin's manifest (cached for 10 s). Invalid ones are skipped."""
    if time.time() - _cache['at'] < 10:
        return _cache['plugins']
    found = []
    core = _core_tier_names()
    if PLUGINS_DIR.is_dir():
        for mf in sorted(PLUGINS_DIR.glob(f'*/{MANIFEST}')):
            try:
                m = validate(json.loads(mf.read_text()), core)
            except Exception:
                continue
            if (mf.parent / 'DISABLED').exists():
                continue
            m['_dir'] = str(mf.parent)
            found.append(m)
    _cache.update(at=time.time(), plugins=found)
    return found


def plugin_tiers() -> Dict[str, Dict[str, Any]]:
    """Permission tiers declared by installed plugins, for mesh/lib/permissions.py."""
    return {p['tier']: {'description': f"Plugin {p['id']} ({p['name']})", 'allow': list(p.get('permissions', []))}
            for p in installed() if p.get('tier')}


def gateway_limits() -> Dict[str, List[str]]:
    """{agent_id: [nginx directive, ...]} for mesh/nginx/generate_config.py."""
    return {p['id']: [l if l.endswith(';') else l + ';' for l in p.get('gateway', [])] for p in installed()}
