"""
Standalone process that keeps the nginx gateway config in sync with the
Agent Registry - not one of the nine A2A agents mesh/start_all.sh manages,
a separate piece of infra you run yourself, same as nginx itself.

Run from the repo root:  python -m mesh.nginx.watcher

Loop: poll the Agent Registry every ADIYAN_NGINX_POLL_SECONDS (default 30,
same default as the registry's own auto-refresh - see
mesh/lib/registry_client.py), re-render generate_config.py's server block,
and only if that actually changed, write it and run `nginx -s reload`. A
poll that finds nothing new never touches nginx at all - no reload storms
from polling itself, only from real registry changes (an agent
registering, deregistering, or changing port).

One-time setup this script depends on but does not do for you (installing
software belongs to you, not an agent process):
  1. Install nginx (`brew install nginx` on macOS). Homebrew's own
     nginx.conf already auto-includes servers/* (confirmed live from its
     post-install caveats), which is exactly where
     generate_config.py's GENERATED_CONFIG_PATH writes to - no manual
     'include' line needed on a Homebrew install. A non-Homebrew install
     needs one 'include' line added to its own http{} block instead, and
     ADIYAN_NGINX_SERVERS_DIR pointed at wherever that is.
  2. Start nginx yourself (`nginx`, or `brew services start nginx`) and
     leave it running, same as this watcher and every other mesh process -
     nothing here ever starts, stops, or kills nginx, only reloads it once
     config content has actually changed, and only after nginx itself is
     already running (a reload attempt against a not-yet-started nginx
     fails harmlessly and is retried next poll, not treated as fatal).

Homebrew's nginx defaults its own docroot server to port 8080 - the
generated gateway server block listens on 8081 instead (see
generate_config.py's DEFAULT_GATEWAY_PORT) so the two never collide.
"""
import asyncio
import logging
import os
import subprocess
import time

from mesh.lib import registry_client
from mesh.nginx.generate_config import render_config, write_if_changed

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(message)s')
logger = logging.getLogger('NginxGatewayWatcher')

DEFAULT_POLL_SECONDS = 30.0


def _poll_seconds() -> float:
    return float(os.environ.get('ADIYAN_NGINX_POLL_SECONDS', str(DEFAULT_POLL_SECONDS)))


def _reload_nginx() -> None:
    try:
        result = subprocess.run(['nginx', '-s', 'reload'], capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            logger.info('nginx reloaded')
        else:
            logger.warning(f'nginx reload failed (is nginx running?): {result.stderr.strip()}')
    except Exception as e:
        logger.warning(f'Could not run nginx -s reload: {e}')


async def _poll_once() -> None:
    agents = await registry_client.list_agents()
    if not agents:
        return
    content = render_config(agents)
    if write_if_changed(content):
        logger.info(f'Gateway config changed ({len(agents)} agent(s)) - reloading nginx')
        _reload_nginx()


def main() -> None:
    interval = _poll_seconds()
    logger.info(f'Watching Agent Registry every {interval}s to keep the nginx gateway in sync')
    while True:
        try:
            asyncio.run(_poll_once())
        except Exception as e:
            logger.warning(f'Poll failed, will retry next cycle: {e}')
        time.sleep(interval)


if __name__ == '__main__':
    main()
