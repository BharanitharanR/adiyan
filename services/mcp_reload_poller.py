"""
Watches services/mcp_registry.py's mcp_servers.json for changes and reloads
MCP tools without a process restart.

Same async start()/stop() shape as every other poller in this codebase
(services/openwa_poller.py, services/kb_ingestion_poller.py,
services/cron_scheduler.py), joined into the same background thread by
main.py. Ticks on the same 60s cadence as the cron scheduler - MCP server
changes are rare and never latency-sensitive, so there's no reason to poll
faster.

The reload mechanism is deliberately simple: two list objects (main.py's
self.mcp_tools and self.owner_mcp_tools) are constructed ONCE and threaded by
REFERENCE into every consumer (LLMAgent, ReasoningCycle, CronScheduler,
OwnerAdminHandler) - see agents/llm_agent.py and services/owner_admin_handler.py
for the `is not None`-not-`or []` fix that makes this safe even when a pool
starts out empty. On a real change, this poller doesn't reassign those
attributes (which would only update main.py's own reference, not any
consumer's) - it mutates the SAME objects in place (clear() + extend()), so
every consumer sees the new tools on its very next tool-bound call, with
nothing to wire up per-consumer.
"""
import asyncio
import logging
from typing import List, Optional

import services.mcp_registry as mcp_registry
from core.mcp_tools import load_mcp_tools, load_owner_mcp_tools

logger = logging.getLogger('MCPReloadPoller')

TICK_INTERVAL_SECONDS = 60.0


class MCPServerPoller:
    def __init__(self, mcp_tools_list: List, owner_mcp_tools_list: List,
                 workspace_mcp_url: Optional[str] = None, owner_email: Optional[str] = None,
                 tick_interval_seconds: float = TICK_INTERVAL_SECONDS):
        self.mcp_tools_list = mcp_tools_list
        self.owner_mcp_tools_list = owner_mcp_tools_list
        self.workspace_mcp_url = workspace_mcp_url
        self.owner_email = owner_email
        self.tick_interval_seconds = tick_interval_seconds
        # Baseline is whatever's on disk right now, not None - main.py's own
        # startup sequence already loaded the current file's contents into both
        # lists before this poller ever starts, so the first tick correctly
        # sees "unchanged" and does nothing, rather than redundantly reloading
        # what was just loaded seconds ago.
        self._last_hash = mcp_registry.file_hash()
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self):
        if self._running:
            logger.warning("MCP reload poller already running")
            return
        self._running = True
        self._task = asyncio.create_task(self._tick_loop())
        logger.info(f"🚀 MCP reload poller started (interval={self.tick_interval_seconds}s)")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("🛑 MCP reload poller stopped")

    async def _tick_loop(self):
        while self._running:
            try:
                await self._tick_once()
            except Exception as e:
                logger.error(f"❌ MCP reload tick failed: {e}", exc_info=True)
            await asyncio.sleep(self.tick_interval_seconds)

    async def _tick_once(self):
        new_hash = mcp_registry.file_hash()
        if new_hash == self._last_hash:
            return  # no-op tick, nothing to log - this is a heartbeat, not an event

        try:
            mcp_registry.list_servers()  # just validates the file parses
        except ValueError as e:
            # Deliberately does NOT update self._last_hash - a still-corrupt file
            # keeps failing this same comparison every tick until it's fixed, so
            # the problem stays visible rather than going silent after one log line.
            logger.error(f"❌ mcp_servers.json changed but won't parse, keeping last-known-good pools: {e}")
            return

        new_client_tools = await load_mcp_tools()
        new_owner_tools = await load_owner_mcp_tools(self.workspace_mcp_url, self.owner_email)

        self.mcp_tools_list.clear()
        self.mcp_tools_list.extend(new_client_tools)
        self.owner_mcp_tools_list.clear()
        self.owner_mcp_tools_list.extend(new_owner_tools)

        self._last_hash = new_hash
        logger.info(
            f"🔄 mcp_servers.json changed - reloaded {len(new_client_tools)} client-facing "
            f"and {len(new_owner_tools)} owner-only tool(s)"
        )
