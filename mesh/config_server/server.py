"""
Config Server - Flask app, combined REST API + dashboard UI for support
teams to view/edit live agent config (prompts, model settings, toggles).

Not an A2A agent - a plain HTTP service (see mesh/config_server/
constants.py for why: a browser dashboard is naturally a REST client, not
an A2A JSON-RPC one). Everything it knows about agent config comes from
calling Config Agent (mesh/config_agent/) over A2A, not from touching
mesh/lib/config_sdk.py or MongoDB directly - see this module's own
`_call_config_agent()` docstring for the technical reason (a Motor/pymongo
async client is bound to the event loop that created it; Flask's
per-request sync model would break that binding), plus the architectural
one: the DB is meant to be wrapped by an agent, not reached into raw from
a second process.

Run from the repo root as `python -m mesh.config_server.server`. Config
Agent (port 8428) and WhatsApp MCP (port 8425, for OTP delivery) should
already be running.
"""
import asyncio
import json
import os
import subprocess
import sys
from functools import wraps
from pathlib import Path

import httpx
from flask import Flask, jsonify, redirect, render_template, request, session, url_for

from mesh.config_agent.constants import AGENT_URL as CONFIG_AGENT_URL
from mesh.config_server import otp
from mesh.config_server.constants import AGENT_STATUS_TIMEOUT_SECONDS, HOST, OLLAMA_URL, OPENWA_DASHBOARD_URL, PORT
from mesh.lib import permissions
from mesh.lib.a2a_client import call_agent
from mesh.lib.paths import eval_reports_dir

app = Flask(__name__)
# Regenerated every process start, not persisted - a login session not
# surviving a restart is the right failure mode for something this
# short-lived (the OTP itself only lives 5 minutes anyway).
app.secret_key = os.urandom(24)

# repo root - mesh/config_server/server.py -> mesh/config_server -> mesh -> root.
# The eval runner is launched as `python -m mesh.evals.eval_analysis`, which
# needs to run from here for that module path to resolve.
_REPO_ROOT = Path(__file__).resolve().parents[2]
EVAL_ENGINE_AGENT_ID = 'eval_engine'

# Tracks the one eval subprocess this dashboard process knows about - in-
# memory only, same short-lived reasoning as the OTP state, and a single
# global (not per-session) is deliberate: only one eval run should ever be
# in flight at a time, competing with itself (and real WhatsApp traffic)
# for the same single-slot Ollama otherwise - confirmed the hard way this
# same session, twice.
_eval_process: 'subprocess.Popen | None' = None


def _call_config_agent(skill_id: str, params: dict) -> dict:
    """Every call opens/closes its own A2A client (see a2a_client.py's own
    _send_and_await) - no persistent, loop-bound resource, so a fresh
    asyncio.run() per Flask request is safe here, unlike a direct MongoDB
    client would be.

    Mints its own 'owner'-tier token - Config Agent is owner-only by design
    (see its skills_catalog.py), and this dashboard's own OTP-gated login
    is what stands in for "acting with owner authority" here, the same way
    Orchestrator mints a token on behalf of whichever WhatsApp sender it
    already resolved a tier for."""
    token = permissions.mint_token('config_server', 'owner')
    return asyncio.run(call_agent(CONFIG_AGENT_URL, skill_id, params, token=token))


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get('authenticated'):
            return redirect(url_for('login'))
        return view(*args, **kwargs)
    return wrapped


@app.route('/login', methods=['GET'])
def login():
    return render_template('login.html')


@app.route('/login/request-otp', methods=['POST'])
def request_otp():
    ok = asyncio.run(otp.send_new_code())
    return jsonify({'ok': ok})


@app.route('/login', methods=['POST'])
def login_submit():
    code = request.form.get('code', '')
    if otp.verify(code):
        session['authenticated'] = True
        return redirect(url_for('landing'))
    return render_template('login.html', error='Incorrect or expired code.')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/register')
def register():
    """Deliberately not @login_required - login itself needs an already-
    registered, already-linked WhatsApp number to send an OTP to, which a
    first-time setup or an owner switching to a different number doesn't
    have yet. This route exists specifically to break that chicken-and-egg
    bind: straight to OpenWA's own dashboard (QR code / session status),
    nothing about this app's own config exposed. Safe to leave open the
    same way the rest of this deployment is - every service here (Config
    Server included) is bound to 127.0.0.1, so reaching this route already
    means local machine access, the same trust boundary the OTP-gated
    routes rely on too."""
    return redirect(OPENWA_DASHBOARD_URL)


@app.route('/')
@login_required
def landing():
    return render_template('landing.html')


@app.route('/configs')
@login_required
def configs():
    return render_template('configs.html')


@app.route('/agents-status')
@login_required
def agents_status():
    return render_template('agents_status.html')


@app.route('/api/agents')
@login_required
def api_agents():
    return jsonify(_call_config_agent('get_all_configs', {}))


def _probe_agent(client: httpx.Client, agent_id: str, host: str, port) -> dict:
    """One agent's live status - name comes off its own agent card (falls
    back to the agent_id if the card can't be reached), same source of
    truth the real A2A callers in this mesh already trust.

    port arrives here having crossed the A2A/protobuf boundary (Config
    Agent's get_all_configs response) - protobuf Struct has no integer
    type, only double, so an int port comes back as e.g. 8427.0 the same
    way mesh/memory/skills/recall.py's top_k did before that fix. int()
    here is the same explicit cast, at the same kind of boundary."""
    url = f'http://{host}:{int(port)}'
    try:
        resp = client.get(f'{url}/.well-known/agent-card.json', timeout=AGENT_STATUS_TIMEOUT_SECONDS)
        resp.raise_for_status()
        name = resp.json().get('name', agent_id)
        return {'agent_id': agent_id, 'name': name, 'url': url, 'online': True}
    except Exception:
        return {'agent_id': agent_id, 'name': agent_id, 'url': url, 'online': False}


# Pseudo-agent_ids that use config_sdk as a config namespace but have no
# actual running server behind them - same category as config_sdk.py's own
# CONTROL_AGENT_ID precedent. Excluded here so they don't show as a
# permanently-red "offline" row for something that was never meant to be
# a live process.
_NON_SERVER_AGENT_IDS = {'eval_engine', '_mesh_control'}


@app.route('/api/agents/status')
@login_required
def api_agents_status():
    """Only agents that have called config_sdk at least once (auto-seeding
    host/port) appear here - same dynamism as /api/agents, and the reason
    an unmigrated agent won't show up until it's wired in, not a bug."""
    all_configs = _call_config_agent('get_all_configs', {}).get('agents', {})
    results = []
    with httpx.Client() as client:
        for agent_id, full in sorted(all_configs.items()):
            if agent_id in _NON_SERVER_AGENT_IDS:
                continue
            constants = full.get('constants', {})
            host, port = constants.get('host'), constants.get('port')
            if host and port:
                results.append(_probe_agent(client, agent_id, host, port))
            else:
                results.append({'agent_id': agent_id, 'name': agent_id, 'url': None, 'online': False})
    return jsonify({'agents': results})


@app.route('/api/agents/<agent_id>/constants/<key>', methods=['PUT'])
@login_required
def api_update_constant(agent_id, key):
    # Pass the value through as-is (bool/int/float/list/str, whatever the
    # dashboard's JSON body actually sent) rather than forcing str() here -
    # Config Agent's update_config skill does the real type coercion,
    # matched against the CURRENT stored type. Blindly stringifying here
    # broke every non-string constant before this fix (a Python list would
    # serialize as "['a', 'b']", not valid JSON).
    value = (request.get_json(silent=True) or {}).get('value', '')
    return jsonify(_call_config_agent('update_config', {'agent_id': agent_id, 'key': key, 'new_value': value}))


@app.route('/api/agents/<agent_id>/stages/<stage_name>', methods=['PUT'])
@login_required
def api_update_stage(agent_id, stage_name):
    body = request.get_json(silent=True) or {}
    return jsonify(_call_config_agent('update_stage_config', {
        'agent_id': agent_id,
        'stage_name': stage_name,
        'model': body.get('model', ''),
        'temperature': float(body.get('temperature', 0.7)),
        'timeout': int(body.get('timeout', 60)),
    }))


@app.route('/api/ollama/models')
@login_required
def api_ollama_models():
    """Real, currently-pulled model names, for the Stages editor's model
    dropdown - hitting Ollama's own /api/tags rather than shelling out to
    `ollama list`, since this process already talks HTTP to everything
    else. Empty list (not an error) if Ollama isn't reachable - the
    dashboard falls back to a plain text input in that case, same
    graceful-degradation rule config_sdk itself follows when Mongo is down."""
    try:
        resp = httpx.get(f'{OLLAMA_URL}/api/tags', timeout=3.0)
        resp.raise_for_status()
        models = [m['name'] for m in resp.json().get('models', [])]
    except Exception:
        models = []
    return jsonify({'models': models})


@app.route('/evals')
@login_required
def evals():
    return render_template('evals.html')


@app.route('/api/evals/run', methods=['POST'])
@login_required
def api_evals_run():
    global _eval_process
    if _eval_process is not None and _eval_process.poll() is None:
        return jsonify({'started': False, 'message': 'A run is already in progress.'})

    log_path = Path.home() / '.Adiyan' / 'logs' / 'eval_run.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, 'w')
    _eval_process = subprocess.Popen(
        [sys.executable, '-m', 'mesh.evals.eval_analysis'],
        cwd=_REPO_ROOT, stdout=log_file, stderr=subprocess.STDOUT,
    )
    return jsonify({'started': True})


@app.route('/api/evals/status')
@login_required
def api_evals_status():
    running = _eval_process is not None and _eval_process.poll() is None
    exit_code = None if running or _eval_process is None else _eval_process.returncode
    return jsonify({'running': running, 'exit_code': exit_code})


@app.route('/api/evals/report')
@login_required
def api_evals_report():
    report_path = eval_reports_dir(EVAL_ENGINE_AGENT_ID) / 'latest.json'
    if not report_path.exists():
        return jsonify({'report': None})
    return jsonify({'report': json.loads(report_path.read_text())})


if __name__ == '__main__':
    app.run(host=HOST, port=PORT)
