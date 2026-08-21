"""
Loaders for the two small per-agent config files every agent under mesh/
carries: mcp_config.json (which MCP servers this agent is a client of) and
runtime_config.json (per-stage local-model settings, e.g. classify_skill /
extract_parameters). One place to read and validate each shape, rather than
each agent re-deriving its own reading/error-handling.
"""
import json
from pathlib import Path
from typing import Any, Dict, List


def load_mcp_config(agent_dir: Path) -> List[str]:
    """Returns the list of MCP server names this agent is a client of. Empty
    list is valid and common - most agents have no MCP dependencies."""
    data = json.loads((agent_dir / 'mcp_config.json').read_text())
    return data.get('mcp_servers', [])


def load_runtime_config(agent_dir: Path) -> Dict[str, Dict[str, Any]]:
    """Returns {stage_name: {model, temperature, timeout}} for this agent.
    Stage names are agent-specific (Scheduler Agent has classify_skill and
    extract_parameters) - this loader doesn't assume which stages exist,
    only the shape each one takes."""
    data = json.loads((agent_dir / 'runtime_config.json').read_text())
    return data.get('stages', {})
