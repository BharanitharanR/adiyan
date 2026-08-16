"""
Bundled Qdrant Service
Manages a Qdrant instance vendored with Adiyan (installer/qdrant-runtime) instead of
depending on whatever Qdrant might already be running on the machine for some other
project - Adiyan owns its own binary, port, and storage directory outright.
"""
import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger('QdrantService')

DATA_DIR = Path.home() / '.Adiyan'
QDRANT_STORAGE_DIR = DATA_DIR / 'qdrant_storage'

# Deliberately not Qdrant's default 6333/6334 - avoids colliding with any other
# Qdrant instance a developer machine might already have running.
DEFAULT_HTTP_PORT = 6339
DEFAULT_GRPC_PORT = 6340
STARTUP_TIMEOUT_SECONDS = 30


def find_qdrant_binary() -> Optional[Path]:
    """Locate the bundled qdrant binary, whether running from source or as the
    PyInstaller-frozen app. When frozen, install.sh copies qdrant-runtime as a
    sibling of the executable - the same layout node-runtime already uses."""
    if getattr(sys, 'frozen', False):
        candidate = Path(sys.executable).resolve().parent / 'qdrant-runtime' / 'qdrant'
    else:
        candidate = Path(__file__).resolve().parent.parent / 'installer' / 'qdrant-runtime' / 'qdrant'
    return candidate if candidate.exists() else None


class QdrantService:
    """Starts/stops Adiyan's own bundled Qdrant process."""

    def __init__(self, port: int = DEFAULT_HTTP_PORT, grpc_port: int = DEFAULT_GRPC_PORT):
        self.port = port
        self.grpc_port = grpc_port
        self.url = f"http://localhost:{port}"
        self._process: Optional[subprocess.Popen] = None

    async def start(self):
        """No-op if our own Qdrant is already answering on this port (e.g. a
        previous Adiyan run that didn't shut down cleanly)."""
        if await self._is_healthy():
            logger.info(f"✅ Bundled Qdrant already running at {self.url}")
            return

        binary = find_qdrant_binary()
        if not binary:
            logger.warning("⚠️  Bundled qdrant binary not found - memory index will be unavailable")
            return

        QDRANT_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        env = {
            **os.environ,
            'QDRANT__STORAGE__STORAGE_PATH': str(QDRANT_STORAGE_DIR),
            'QDRANT__SERVICE__HTTP_PORT': str(self.port),
            'QDRANT__SERVICE__GRPC_PORT': str(self.grpc_port),
            'QDRANT__LOG_LEVEL': 'WARN',
        }
        self._process = subprocess.Popen(
            [str(binary)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info(f"🚀 Started bundled Qdrant (pid {self._process.pid}) on port {self.port}")

        for _ in range(STARTUP_TIMEOUT_SECONDS * 2):
            if await self._is_healthy():
                logger.info(f"✅ Bundled Qdrant ready at {self.url}")
                return
            if self._process.poll() is not None:
                raise RuntimeError(f"Bundled Qdrant exited immediately (code {self._process.returncode})")
            await asyncio.sleep(0.5)

        raise RuntimeError(f"Bundled Qdrant didn't become healthy within {STARTUP_TIMEOUT_SECONDS}s")

    async def _is_healthy(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(f"{self.url}/collections")
                return resp.status_code == 200
        except Exception:
            return False

    def stop(self):
        """Only stops the process if we started it - never touches a Qdrant that
        was already running before start() (that one isn't ours to kill)."""
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            logger.info("🛑 Bundled Qdrant stopped")
