"""Loads LangChain-compatible tools from local MCP servers for LLMAgent's tool-calling loop.

Search/fetch capability comes from the duckduckgo-mcp-server binary (stdio transport,
no API key required) rather than any Adiyan-specific scraping code - the MCP server
owns that logic entirely.
"""
import shutil
import logging
from typing import List

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

logger = logging.getLogger('MCPTools')

DUCKDUCKGO_MCP_COMMAND = "duckduckgo-mcp-server"


async def load_mcp_tools() -> List[BaseTool]:
    """Load tools from configured MCP servers. Returns [] (not an error) if a
    server binary isn't installed, so Adiyan still runs without tool-calling."""
    command = shutil.which(DUCKDUCKGO_MCP_COMMAND)
    if not command:
        logger.warning(f"⚠️  {DUCKDUCKGO_MCP_COMMAND} not found on PATH - LLM will run without tools")
        return []

    try:
        client = MultiServerMCPClient({
            "duckduckgo": {
                "command": command,
                "args": [],
                "transport": "stdio",
            }
        })
        tools = await client.get_tools()
        logger.info(f"✅ Loaded {len(tools)} MCP tool(s): {[t.name for t in tools]}")
        return tools
    except Exception as e:
        logger.error(f"❌ Failed to load MCP tools: {e}")
        return []
