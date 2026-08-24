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
import os
from functools import wraps

from flask import Flask, jsonify, redirect, render_template, request, session, url_for

from mesh.config_agent.constants import AGENT_URL as CONFIG_AGENT_URL
from mesh.config_server import otp
from mesh.config_server.constants import HOST, PORT
from mesh.lib import permissions
from mesh.lib.a2a_client import call_agent

app = Flask(__name__)
# Regenerated every process start, not persisted - a login session not
# surviving a restart is the right failure mode for something this
# short-lived (the OTP itself only lives 5 minutes anyway).
app.secret_key = os.urandom(24)


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
        return redirect(url_for('dashboard'))
    return render_template('login.html', error='Incorrect or expired code.')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/')
@login_required
def dashboard():
    return render_template('dashboard.html')


@app.route('/api/agents')
@login_required
def api_agents():
    return jsonify(_call_config_agent('get_all_configs', {}))


@app.route('/api/agents/<agent_id>/constants/<key>', methods=['PUT'])
@login_required
def api_update_constant(agent_id, key):
    value = (request.get_json(silent=True) or {}).get('value', '')
    return jsonify(_call_config_agent('update_config', {'agent_id': agent_id, 'key': key, 'new_value': str(value)}))


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


if __name__ == '__main__':
    app.run(host=HOST, port=PORT)
