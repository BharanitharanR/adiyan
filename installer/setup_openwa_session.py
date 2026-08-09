#!/usr/bin/env python3
"""
First-run OpenWA bootstrap: wait for OpenWA to come up, discover its
bootstrap API key, create the Adiyan session if it doesn't exist yet, and
write both into ~/.Adiyan/pipeline.json.

Safe to re-run: does nothing if pipeline.json already has a real API key
configured, and reuses an existing session by name instead of duplicating it.

Usage: python3 setup_openwa_session.py <openwa_working_dir> [--url http://localhost:2785] [--session-name executive-coach]
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PIPELINE_FILE = Path.home() / '.Adiyan' / 'pipeline.json'


def log(msg):
    print(f"[setup_openwa_session] {msg}", file=sys.stderr)


def http_json(method, url, api_key=None, body=None, timeout=10):
    headers = {'Content-Type': 'application/json'}
    if api_key:
        headers['X-API-Key'] = api_key
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        # The server's error body (usually a JSON message) is the actual diagnostic - urllib
        # discards it by default and just raises the status code, which isn't enough to debug with.
        detail = e.read().decode(errors='replace')
        raise RuntimeError(f"{method} {url} -> HTTP {e.code}: {detail}") from None
    except (urllib.error.URLError, OSError) as e:
        # Connection-level failure (refused, reset, timed out) rather than an HTTP error response -
        # most commonly the OpenWA process itself dying mid-request (e.g. killed by the OS under
        # memory pressure while launching Chromium for a session). Not an HTTPError, so it has to be
        # caught separately - otherwise it crashes with a raw traceback and skips the retry loop
        # in ensure_started() entirely instead of getting a chance to recover.
        raise RuntimeError(f"{method} {url} -> connection failed: {e}") from None


def wait_for_openwa(base_url, timeout_s=60):
    log(f"Waiting for OpenWA at {base_url}...")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            http_json('GET', f'{base_url}/api/health', timeout=3)
            log("OpenWA is up")
            return
        except Exception:
            time.sleep(1)
    raise RuntimeError(f"OpenWA did not become ready within {timeout_s}s")


def wait_for_bootstrap_key(openwa_dir: Path, timeout_s=30) -> str:
    key_file = openwa_dir / 'data' / '.api-key'
    log(f"Waiting for bootstrap key at {key_file}...")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if key_file.exists():
            key = key_file.read_text().strip()
            if key:
                log("Bootstrap key found")
                return key
        time.sleep(1)
    raise RuntimeError(f"Bootstrap key file did not appear within {timeout_s}s: {key_file}")


def ensure_started(base_url, api_key, session_id, status):
    # A session sits in CREATED (or DISCONNECTED/ACTION_REQUIRED) until
    # explicitly started - it will never produce a QR code otherwise.
    # AUTO_START_SESSIONS only reconnects sessions that were PREVIOUSLY
    # authenticated; it does nothing for a session that's never been linked.
    if status in ('ready', 'qr_ready', 'initializing', 'authenticating'):
        log(f"Session status is '{status}' - already starting/started")
        return
    log(f"Session status is '{status}' - starting it...")
    # A session that was JUST created can 500 on an immediate start - the server side hasn't
    # finished settling the new row yet. Retry a few times before giving up for real.
    attempts = 5
    for attempt in range(1, attempts + 1):
        try:
            http_json('POST', f'{base_url}/api/sessions/{session_id}/start', api_key=api_key)
            log("Start requested")
            return
        except RuntimeError as e:
            if attempt == attempts:
                raise
            # Tell a transient app-level hiccup (still up, briefly 500s) apart from the process
            # actually being gone (e.g. killed by the OS under memory pressure while launching
            # Chromium) - the latter needs a very different diagnosis than "just retrying".
            try:
                http_json('GET', f'{base_url}/api/health', timeout=3)
                still_up = True
            except Exception:
                still_up = False
            if not still_up:
                log(f"Start attempt {attempt}/{attempts} failed ({e}) - and OpenWA's health check "
                    f"is ALSO failing, meaning the process itself died (not just this request). On an "
                    f"8GB machine this is most likely the OS killing it under memory pressure while it "
                    f"tries to launch Chromium for the session. Retrying anyway in case something "
                    f"restarts it, but this may need more free RAM (close other apps) to succeed.")
            else:
                log(f"Start attempt {attempt}/{attempts} failed ({e}) - retrying in 2s...")
            time.sleep(2)


def find_or_create_session(base_url, api_key, session_name) -> str:
    sessions = http_json('GET', f'{base_url}/api/sessions', api_key=api_key)
    for s in sessions:
        if s.get('name') == session_name:
            log(f"Session '{session_name}' already exists (id={s['id']}, status={s.get('status')})")
            ensure_started(base_url, api_key, s['id'], s.get('status'))
            return s['id']

    log(f"Creating session '{session_name}'...")
    created = http_json('POST', f'{base_url}/api/sessions', api_key=api_key, body={'name': session_name})
    log(f"Created session '{session_name}' (id={created['id']})")
    ensure_started(base_url, api_key, created['id'], created.get('status'))
    return created['id']


def already_configured() -> bool:
    if not PIPELINE_FILE.exists():
        return False
    data = json.loads(PIPELINE_FILE.read_text())
    return bool(data.get('openwa_api_key'))


def write_config(api_key, session_name, base_url):
    PIPELINE_FILE.parent.mkdir(exist_ok=True)
    data = json.loads(PIPELINE_FILE.read_text()) if PIPELINE_FILE.exists() else {}
    data['openwa_api_key'] = api_key
    data['openwa_session_name'] = session_name
    data['openwa_url'] = base_url
    PIPELINE_FILE.write_text(json.dumps(data, indent=2))
    log(f"Wrote OpenWA config to {PIPELINE_FILE}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('openwa_dir', help="OpenWA's working directory (where it writes data/.api-key)")
    parser.add_argument('--url', default='http://localhost:2785')
    parser.add_argument('--session-name', default='executive-coach')
    args = parser.parse_args()

    if already_configured():
        log("pipeline.json already has an OpenWA API key configured - nothing to do")
        return

    wait_for_openwa(args.url)
    api_key = wait_for_bootstrap_key(Path(args.openwa_dir))
    try:
        find_or_create_session(args.url, api_key, args.session_name)
    except RuntimeError as e:
        log(f"ERROR: {e}")
        log(f"OpenWA's own log has the underlying error: {Path.home() / '.Adiyan' / 'openwa.log'}")
        log("This step is safe to re-run: python3 setup_openwa_session.py <openwa_dir>")
        sys.exit(1)
    write_config(api_key, args.session_name, args.url)
    log("Done. Next: open the dashboard and scan the QR code to link WhatsApp.")


if __name__ == '__main__':
    main()
