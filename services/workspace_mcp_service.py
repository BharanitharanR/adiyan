"""
Persistent workspace-mcp (Gmail/Calendar) HTTP server.

Google's OAuth consent flow redirects back to a local URL some indeterminate time
after the auth link is generated - anywhere from a few seconds to a few minutes,
paced by the owner actually clicking through Google's sign-in and consent screens.
A stdio MCP server spun up fresh per tool call (the pattern every other MCP
integration in this codebase uses - core/mcp_tools.py's duckduckgo/crawl4ai pools)
tears its process down - and with it, the local OAuth callback listener - the
moment that one tool call returns, almost always before the owner has finished
the browser flow. Confirmed live: Google's consent screen completed successfully,
but the redirect landed on a closed port with nothing listening.

The fix: run workspace-mcp as its own persistent background process, like
services/qdrant_service.py's bundled Qdrant, in --transport streamable-http mode -
so the OAuth listener, and the server itself, stay alive independent of any single
tool call's lifetime. core/mcp_tools.py's load_owner_mcp_tools() connects to it
over HTTP instead of spawning a stdio subprocess per call.

Owner-only, same as everything else this process serves - see core/mcp_tools.py's
module docstring for why this must never be reachable from client-facing code.
Only started at all if Google credentials are present in the vault
(config/secrets_vault.py); otherwise there's nothing worth keeping a server up for.
"""
import asyncio
import logging
import os
import shutil
import subprocess
from typing import Optional

import httpx

logger = logging.getLogger('WorkspaceMCPService')

DEFAULT_PORT = 8000
STARTUP_TIMEOUT_SECONDS = 20


class WorkspaceMCPService:
    """Starts/stops a persistent workspace-mcp HTTP server for Gmail/Calendar."""

    def __init__(self, client_id: str, client_secret: str, port: int = DEFAULT_PORT,
                 owner_email: Optional[str] = None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.port = port
        self.owner_email = owner_email
        self.url = f"http://127.0.0.1:{port}/mcp"
        self._process: Optional[subprocess.Popen] = None

    async def start(self):
        """No-op if a workspace-mcp is already answering on this port - same
        pattern as QdrantService.start(), so a restart doesn't leave a dangling
        duplicate process behind."""
        if await self._is_healthy():
            logger.info(f"✅ workspace-mcp already running at {self.url}")
            return

        command = shutil.which("workspace-mcp")
        if not command:
            logger.warning("⚠️  workspace-mcp not installed - Gmail/Calendar tools unavailable")
            return

        env = {
            **os.environ,
            'GOOGLE_OAUTH_CLIENT_ID': self.client_id,
            'GOOGLE_OAUTH_CLIENT_SECRET': self.client_secret,
            'WORKSPACE_MCP_PORT': str(self.port),
            'GOOGLE_OAUTH_REDIRECT_URI': f'http://localhost:{self.port}/oauth2callback',
            # Required by the underlying oauthlib because the callback is plain
            # http://localhost, not https - fine for a same-machine loopback
            # redirect, not something ever exposed externally.
            'OAUTHLIB_INSECURE_TRANSPORT': '1',
        }
        if self.owner_email:
            # Confirmed live bug this fixes: without this, workspace-mcp exposes
            # user_google_email as a required tool parameter the admin LLM must
            # supply itself - and a small local model, with no reliable way to
            # recall the owner's real address across turns, guessed a placeholder
            # ("owner@example.com") instead. That wrong-but-non-null email defeats
            # the credential store's single-user auto-detection (which only
            # activates when the parameter is omitted entirely), so every tool
            # call after the first missed the already-stored real credentials and
            # re-triggered the OAuth flow - an infinite re-auth loop. Setting this
            # env var makes workspace-mcp itself strip user_google_email from the
            # tool schema it hands to the LLM and inject the correct default, so
            # the model never has an opportunity to get it wrong.
            env['USER_GOOGLE_EMAIL'] = self.owner_email
        self._process = subprocess.Popen(
            [command, '--transport', 'streamable-http', '--single-user',
             '--tools', 'gmail', 'calendar', '--read-only'],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        logger.info(f"🚀 Started workspace-mcp (pid {self._process.pid}) on port {self.port}")

        for _ in range(STARTUP_TIMEOUT_SECONDS * 2):
            if await self._is_healthy():
                logger.info(f"✅ workspace-mcp ready at {self.url}")
                return
            if self._process.poll() is not None:
                logger.error(f"❌ workspace-mcp exited immediately (code {self._process.returncode})")
                return
            await asyncio.sleep(0.5)
        logger.error(f"❌ workspace-mcp didn't become healthy within {STARTUP_TIMEOUT_SECONDS}s")

    async def _is_healthy(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                # A streamable-http MCP endpoint doesn't answer a plain GET with a
                # normal 200 - any response short of a server error still proves
                # the process is up and the port is bound, which is all a
                # liveness check needs (the real MCP handshake happens separately,
                # via MultiServerMCPClient, when tools are actually loaded).
                resp = await client.get(self.url)
                return resp.status_code < 500
        except Exception:
            return False

    def stop(self):
        """Only stops the process if we started it - never touches a server we
        found already healthy and skipped starting (not ours to kill)."""
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            logger.info("🛑 workspace-mcp stopped")
