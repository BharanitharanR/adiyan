import asyncio
import hmac
from datetime import datetime, timedelta
from flask import Flask, Response, jsonify, request, send_file
from flask_cors import CORS
from config.control_plane import ControlPlane
from config.secrets_vault import get_secret
import config.database as db
import logging
from pathlib import Path
import requests
import pika
import json
import os
import uuid
import time
import io
import qrcode

# Setup
app = Flask(__name__, static_folder=str(Path(__file__).parent), static_url_path='')
CORS(app)
control_plane = ControlPlane()

logger = logging.getLogger('ControlPanel')

DASHBOARD_USERNAME_KEY = 'DASHBOARD_USERNAME'
DASHBOARD_PASSWORD_KEY = 'DASHBOARD_PASSWORD'
DASHBOARD_DEFAULT_USERNAME = 'owner'

_warned_unprotected = False


@app.before_request
def _require_dashboard_auth():
    """Gates every route (dashboard HTML, static assets, and the whole API) behind HTTP
    Basic Auth once a password is set via tools/set_secret.py DASHBOARD_PASSWORD - the
    browser's own native login prompt handles it, no custom login page needed, and once
    authenticated for this origin the browser attaches the same credentials to every
    fetch() the dashboard's JS makes automatically.

    Confirmed live: this control panel was being reached over an ngrok tunnel with zero
    access control - anyone with the URL could view or change every agent config, client,
    and persona. Deliberately open (with a one-time warning, not a hard failure) when no
    password is configured yet, so a fresh local-only install isn't locked out of its own
    dashboard before the owner has had a chance to set one."""
    global _warned_unprotected
    stored_password = get_secret(DASHBOARD_PASSWORD_KEY)
    if not stored_password:
        if not _warned_unprotected:
            logger.warning(
                "⚠️  No dashboard password set - the control panel is reachable by anyone "
                "with its URL (e.g. an ngrok tunnel), with no login required. Run "
                "'python3 tools/set_secret.py DASHBOARD_PASSWORD' to secure it."
            )
            _warned_unprotected = True
        return None

    stored_username = get_secret(DASHBOARD_USERNAME_KEY) or DASHBOARD_DEFAULT_USERNAME
    auth = request.authorization
    if (
        auth
        and hmac.compare_digest(auth.username, stored_username)
        and hmac.compare_digest(auth.password, stored_password)
    ):
        return None

    return Response(
        'Authentication required', 401,
        {'WWW-Authenticate': 'Basic realm="Adiyan Dashboard"'},
    )

def _load_personas_file() -> dict:
    """Same {'active_persona', 'personas': {id: {name, system_prompt}}} shape the routes
    below already work with - now backed by config/database.py's personas table
    instead of personas.json. Name kept for the routes' sake; not actually a file anymore."""
    return {
        'active_persona': db.get_active_persona_id(),
        'personas': {
            pid: {'name': p['name'], 'system_prompt': p['system_prompt']}
            for pid, p in db.get_personas().items()
        },
    }

def _save_personas_file(data: dict):
    """Reconciles the db with the given full-state dict: creates/updates every persona
    present, deletes any that were removed, and sets the active one. Routes below build
    `data` by mutating the dict from _load_personas_file() then calling this - same
    create/update/delete/activate semantics as the old read-modify-write-whole-file flow."""
    existing_ids = set(db.get_personas().keys())
    incoming = data.get('personas', {})
    for pid in existing_ids - set(incoming.keys()):
        db.delete_persona(pid)
    for pid, p in incoming.items():
        db.upsert_persona(pid, p.get('name', pid), p.get('system_prompt', ''))
    active = data.get('active_persona')
    if active:
        db.set_active_persona(active)

def get_ollama_models():
    """Fetch available models from Ollama"""
    try:
        ollama_url = control_plane.config.ollama_url
        response = requests.get(f"{ollama_url}/api/tags", timeout=5)
        if response.status_code == 200:
            data = response.json()
            return [model['name'] for model in data.get('models', [])]
        return []
    except Exception as e:
        logger.warning(f"Failed to fetch Ollama models: {e}")
        return []

# ============= API ENDPOINTS =============

@app.route('/', methods=['GET'])
def dashboard():
    """Serve dashboard"""
    dashboard_path = Path(__file__).parent / 'dashboard.html'
    return send_file(dashboard_path, mimetype='text/html')

@app.route('/api/health', methods=['GET'])
def health():
    """Health check"""
    return jsonify({'status': 'ok', 'service': 'Adiyan Control Plane'})

@app.route('/webhook/openwa', methods=['POST'])
def openwa_webhook():
    """Receive webhooks from OpenWA and process through orchestrator"""
    try:
        webhook_data = request.get_json()

        if not webhook_data:
            logger.warning("Empty webhook data received")
            return jsonify({'status': 'received'}), 202

        # Log webhook received
        event = webhook_data.get('event', 'unknown')
        logger.info(f"📬 Received OpenWA webhook: {event}")

        # For now, just acknowledge
        # TODO: Process through orchestrator asynchronously
        return jsonify({'status': 'received', 'event': event}), 202

    except Exception as e:
        logger.error(f"❌ Webhook error: {str(e)}", exc_info=True)
        return jsonify({'error': str(e)}), 500

@app.route('/api/ollama/models', methods=['GET'])
def get_models():
    """Get list of available Ollama models"""
    models = get_ollama_models()
    return jsonify({'models': models})

@app.route('/api/agents', methods=['GET'])
def get_agents():
    """Get all agents and their configuration"""
    agents = {}
    for name, config in control_plane.get_all_configs().items():
        agents[name] = {
            'name': config.name,
            'kind': config.kind,
            'enabled': config.enabled,
            'tools': config.tools,
            'model': config.model,
            'temperature': config.temperature,
            'timeout': config.timeout,
            'prompt_template': config.prompt_template,
        }
    return jsonify(agents)

@app.route('/api/agents/<agent_name>', methods=['GET'])
def get_agent(agent_name):
    """Get single agent configuration"""
    config = control_plane.get_agent_config(agent_name)
    if not config:
        return jsonify({'error': f'Agent {agent_name} not found'}), 404

    return jsonify({
        'name': config.name,
        'kind': config.kind,
        'enabled': config.enabled,
        'tools': config.tools,
        'model': config.model,
        'temperature': config.temperature,
        'timeout': config.timeout,
        'prompt_template': config.prompt_template,
        'custom_params': config.custom_params
    })

@app.route('/api/agents/<agent_name>/enable', methods=['POST'])
def enable_agent(agent_name):
    """Enable agent"""
    if control_plane.enable_agent(agent_name):
        return jsonify({'status': 'enabled', 'agent': agent_name})
    return jsonify({'error': 'Failed to enable agent'}), 400

@app.route('/api/agents/<agent_name>/disable', methods=['POST'])
def disable_agent(agent_name):
    """Disable agent"""
    if control_plane.disable_agent(agent_name):
        return jsonify({'status': 'disabled', 'agent': agent_name})
    return jsonify({'error': 'Failed to disable agent'}), 400

@app.route('/api/agents/<agent_name>/tools', methods=['PUT'])
def update_agent_tools(agent_name):
    """Update agent tools"""
    data = request.json
    tools = data.get('tools', [])

    if control_plane.update_agent_tools(agent_name, tools):
        return jsonify({'status': 'updated', 'agent': agent_name, 'tools': tools})
    return jsonify({'error': 'Failed to update tools'}), 400

@app.route('/api/agents/<agent_name>/model', methods=['PUT'])
def update_agent_model(agent_name):
    """Update agent model/temperature/timeout, and prompt_template for the reasoning-cycle
    agents (Hermes, Prometheus, Pythia, Hephaestus, Calliope, Momus)."""
    data = request.json
    fields = {
        'model': data.get('model'),
        'temperature': data.get('temperature', 0.7),
        'timeout': data.get('timeout', 60),
    }
    if 'prompt_template' in data:
        fields['prompt_template'] = data.get('prompt_template')

    if control_plane.update_agent_config(agent_name, **fields):
        return jsonify({'status': 'updated', 'agent': agent_name, **fields})
    return jsonify({'error': 'Agent not found'}), 404

@app.route('/api/personas', methods=['GET'])
def get_personas():
    """List all personas and which one is active"""
    data = _load_personas_file()
    return jsonify({
        'active_persona': data.get('active_persona'),
        'personas': {
            pid: {'id': pid, 'name': p.get('name', ''), 'system_prompt': p.get('system_prompt', '')}
            for pid, p in data.get('personas', {}).items()
        }
    })

@app.route('/api/personas/<persona_id>', methods=['GET'])
def get_persona(persona_id):
    """Get a single persona"""
    data = _load_personas_file()
    persona = data.get('personas', {}).get(persona_id)
    if not persona:
        return jsonify({'error': f'Persona {persona_id} not found'}), 404
    return jsonify({'id': persona_id, 'name': persona.get('name', ''), 'system_prompt': persona.get('system_prompt', '')})

@app.route('/api/personas', methods=['POST'])
def create_persona():
    """Create a new persona"""
    body = request.json or {}
    persona_id = (body.get('id') or '').strip()
    name = (body.get('name') or '').strip()
    system_prompt = (body.get('system_prompt') or '').strip()

    if not persona_id or not name or not system_prompt:
        return jsonify({'error': 'id, name, and system_prompt are required'}), 400
    if persona_id == 'system':
        return jsonify({'error': "'system' is a reserved persona id"}), 400

    data = _load_personas_file()
    personas = data.setdefault('personas', {})
    if persona_id in personas:
        return jsonify({'error': f'Persona {persona_id} already exists'}), 409

    personas[persona_id] = {'name': name, 'system_prompt': system_prompt}
    if not data.get('active_persona'):
        data['active_persona'] = persona_id
    _save_personas_file(data)
    logger.info(f"Created persona '{persona_id}'")
    return jsonify({'status': 'created', 'id': persona_id, 'name': name, 'system_prompt': system_prompt}), 201

@app.route('/api/personas/<persona_id>', methods=['PUT'])
def update_persona(persona_id):
    """Update a persona's name/system_prompt"""
    if persona_id == 'system':
        return jsonify({'error': "'system' persona cannot be edited via API"}), 400

    data = _load_personas_file()
    personas = data.get('personas', {})
    if persona_id not in personas:
        return jsonify({'error': f'Persona {persona_id} not found'}), 404

    body = request.json or {}
    name = (body.get('name') or '').strip()
    system_prompt = (body.get('system_prompt') or '').strip()
    if not name or not system_prompt:
        return jsonify({'error': 'name and system_prompt are required'}), 400

    personas[persona_id] = {'name': name, 'system_prompt': system_prompt}
    _save_personas_file(data)
    logger.info(f"Updated persona '{persona_id}'")
    return jsonify({'status': 'updated', 'id': persona_id, 'name': name, 'system_prompt': system_prompt})

@app.route('/api/personas/<persona_id>', methods=['DELETE'])
def delete_persona(persona_id):
    """Delete a persona (not allowed for the active persona or the reserved 'system' id)"""
    if persona_id == 'system':
        return jsonify({'error': "'system' persona cannot be deleted"}), 400

    data = _load_personas_file()
    personas = data.get('personas', {})
    if persona_id not in personas:
        return jsonify({'error': f'Persona {persona_id} not found'}), 404
    if data.get('active_persona') == persona_id:
        return jsonify({'error': 'Cannot delete the active persona; activate a different one first'}), 400

    del personas[persona_id]
    _save_personas_file(data)
    logger.info(f"Deleted persona '{persona_id}'")
    return jsonify({'status': 'deleted', 'id': persona_id})

@app.route('/api/personas/<persona_id>/activate', methods=['POST'])
def activate_persona(persona_id):
    """Set which persona is active"""
    if persona_id == 'system':
        return jsonify({'error': "'system' persona cannot be activated"}), 400

    data = _load_personas_file()
    if persona_id not in data.get('personas', {}):
        return jsonify({'error': f'Persona {persona_id} not found'}), 404

    data['active_persona'] = persona_id
    _save_personas_file(data)
    logger.info(f"Activated persona '{persona_id}'")
    return jsonify({'status': 'activated', 'active_persona': persona_id})

@app.route('/api/config', methods=['GET'])
def get_config():
    """Get entire pipeline configuration"""
    return jsonify(control_plane.config.to_dict())

@app.route('/api/config/system', methods=['PUT'])
def update_system_config():
    """Update system-level settings"""
    data = request.json

    for key, value in data.items():
        if control_plane.update_system_setting(key, value):
            pass

    return jsonify({'status': 'updated', 'config': control_plane.config.to_dict()})

@app.route('/api/config/export', methods=['GET'])
def export_config():
    """Export configuration as JSON"""
    return jsonify(control_plane.config.to_dict())

@app.route('/api/google-workspace/status', methods=['GET'])
def google_workspace_status():
    """Owner-only Gmail/Calendar connection status (services/owner_admin_handler.py's
    check_google_workspace_status admin tool is the WhatsApp equivalent of this)."""
    from core.mcp_tools import is_google_workspace_configured
    tool_count = app.config.get('OWNER_MCP_TOOL_COUNT', 0)
    if not is_google_workspace_configured():
        return jsonify({'status': 'not_configured', 'tool_count': 0})
    if tool_count == 0:
        return jsonify({'status': 'configured_but_unavailable', 'tool_count': 0})
    return jsonify({'status': 'connected', 'tool_count': tool_count})

@app.route('/api/routines', methods=['GET'])
def list_routines():
    """The full routines library, each merged with its file content (schedule,
    target, instructions) - config/database.py's routines table alone only has
    name/description, not enough to show what a routine actually does. Small
    library (a handful to maybe dozens of routines for one business), so
    reading every file per request is cheap - no caching needed."""
    from services.routine_store import get_full_details
    routines = []
    for row in db.list_routines():
        details = get_full_details(row)
        routines.append(details or {'name': row['name'], 'description': row['description'], 'error': 'file missing'})
    return jsonify(routines)

@app.route('/api/token-usage', methods=['GET'])
def token_usage():
    """Real per-call token counts (config/database.py's token_usage table,
    populated from core/token_usage.py's instrumentation at every LLM call
    site) - the concrete "this is what stays on your own hardware" number,
    broken down by routine (the specific granularity asked for) and by which
    part of the system spent it. ?days=N filters to the trailing window
    (default: all-time)."""
    days = request.args.get('days', type=int)
    since = None
    if days:
        since = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%S')
    return jsonify({
        'total': db.token_usage_total(since=since),
        'by_routine': db.token_usage_by_routine(since=since),
        'by_context_type': db.token_usage_by_context_type(since=since),
        'recent': db.list_token_usage(limit=50),
    })

class _CapturingOpenWA:
    """A fake OpenWAService for test endpoints below - records what WOULD have
    been sent instead of touching the real WhatsApp connection, so routines/
    jobs/admin flows can be exercised repeatedly from a test suite without
    spamming a real chat or needing WhatsApp linked at all."""
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, message):
        self.sent.append({'chat_id': chat_id, 'message': message})
        return {'messageId': f'test-{len(self.sent)}'}


@app.route('/api/test/owner-message', methods=['POST'])
def test_owner_message():
    """Drives the exact same owner self-chat routing a real WhatsApp message
    would (trigger phrase -> pending job response -> admin agent fallback -
    services/kb_ingestion_poller.py's _handle_message) without WhatsApp.

    Reuses the live kb_poller/admin_handler singletons (so routine/job state
    changes are real, checkable via /api/routines etc.) but temporarily swaps
    both their .openwa references to a capturing fake for the duration of this
    one call, so nothing gets sent to your real WhatsApp. Restored in a
    finally block even on error. Note: this briefly shares mutable state with
    the live background poller (which ticks every ~20s) - a real incoming
    owner message arriving in that exact window could interleave. Acceptable
    for a local, single-owner dev/test workflow; not something to run under
    concurrent load."""
    kb_poller = app.config.get('KB_POLLER')
    if not kb_poller or not kb_poller.admin_handler:
        return jsonify({'error': 'KB poller / admin handler not available yet (still starting up?)'}), 503

    message = (request.json or {}).get('message', '')
    if not message:
        return jsonify({'error': 'message is required'}), 400

    # CronScheduler keeps its OWN openwa reference, set once in main.py at
    # construction - entirely separate from kb_poller.openwa and
    # admin_handler.openwa. Confirmed live: missing this swap let a real job
    # (created via this exact test endpoint, "reuse an existing routine"
    # path) send a genuine WhatsApp message through the real connection
    # instead of the fake - the routing logic ran correctly, only the actual
    # send escaped the fake. All three references must be swapped together.
    cron_scheduler = kb_poller.admin_handler.cron_scheduler
    fake_openwa = _CapturingOpenWA()
    original_kb_openwa = kb_poller.openwa
    original_admin_openwa = kb_poller.admin_handler.openwa
    original_scheduler_openwa = cron_scheduler.openwa if cron_scheduler else None
    original_owner_chat_id = kb_poller._owner_chat_id
    kb_poller.openwa = fake_openwa
    kb_poller.admin_handler.openwa = fake_openwa
    if cron_scheduler:
        cron_scheduler.openwa = fake_openwa
    kb_poller._owner_chat_id = 'test-owner-chat'
    try:
        import uuid as _uuid
        fake_message = {
            'id': f'test-{_uuid.uuid4()}',
            'timestamp': int(time.time()) + 1,
            'body': message,
            'type': 'chat',
        }
        asyncio.run(kb_poller._handle_message(fake_message))
    except Exception as e:
        return jsonify({'error': str(e) or type(e).__name__}), 500
    finally:
        kb_poller.openwa = original_kb_openwa
        kb_poller.admin_handler.openwa = original_admin_openwa
        if cron_scheduler:
            cron_scheduler.openwa = original_scheduler_openwa
        kb_poller._owner_chat_id = original_owner_chat_id

    return jsonify({'sent_messages': fake_openwa.sent})


@app.route('/api/test/client-message', methods=['POST'])
def test_client_message():
    """Drives a real client message through the full 7-agent pipeline
    (Parser -> Validator -> Router -> LLM -> Synthesizer -> Storage ->
    Publisher), exactly as OpenWAPoller would, without WhatsApp. Body:
    {"contact_name": str, "message": str, "lid": str (optional)}. Unlike the
    owner-message test above, this does NOT fake the WhatsApp send (the
    Publisher stage sends via whatever whatsapp_sender the orchestrator was
    built with) - harmless against a real test client's own chat, and avoids
    reimplementing Publisher's registration/error/normal-reply branching here.
    Returns the actual response text (state.llm_response)."""
    orchestrator = app.config.get('ORCHESTRATOR')
    if not orchestrator:
        return jsonify({'error': 'Orchestrator not available yet (still starting up?)'}), 503

    data = request.json or {}
    contact_name = data.get('contact_name')
    message = data.get('message', '')
    if not contact_name or not message:
        return jsonify({'error': 'contact_name and message are required'}), 400

    from core.base_agent import AgentState
    import uuid as _uuid
    state = AgentState(
        message_id=f'test-{_uuid.uuid4()}',
        contact_name=contact_name,
        lid=data.get('lid', f'{contact_name}@test'),
        message_body=message,
    )
    try:
        result = asyncio.run(orchestrator.execute_pipeline(state))
    except Exception as e:
        return jsonify({'error': str(e) or type(e).__name__}), 500

    return jsonify({
        'response': result.llm_response,
        'error': result.error,
        'is_registration': result.is_registration,
        'is_job_response': result.is_job_response,
        'metadata': result.metadata,
    })


@app.route('/api/dashboard-auth/status', methods=['GET'])
def dashboard_auth_status():
    """Whether the dashboard is actually password-protected right now. Reachable even
    when unprotected (there's nothing to gate at that point anyway) so the dashboard can
    show a warning banner precisely when it's most needed - before a password is set."""
    return jsonify({'protected': bool(get_secret(DASHBOARD_PASSWORD_KEY))})

@app.route('/api/test-message', methods=['POST'])
def test_message():
    """Test endpoint - send a message through the orchestrator"""
    data = request.json

    # Validate input
    if not data or 'message_body' not in data:
        return jsonify({'error': 'message_body is required'}), 400

    contact_name = data.get('contact_name', 'TestUser')
    lid = data.get('lid', 'test_' + str(int(time.time())))
    message_body = data.get('message_body')

    # Publish to RabbitMQ for processing
    try:
        connection = pika.BlockingConnection(
            pika.URLParameters(control_plane.config.rabbitmq_url)
        )
        channel = connection.channel()
        channel.exchange_declare(exchange='adiyan', exchange_type='topic', durable=True)

        message = {
            'message_id': str(uuid.uuid4()),
            'contact_name': contact_name,
            'lid': lid,
            'message_body': message_body
        }

        channel.basic_publish(
            exchange='adiyan',
            routing_key='messages.incoming',
            body=json.dumps(message),
            properties=pika.BasicProperties(delivery_mode=2)
        )

        connection.close()

        return jsonify({
            'status': 'sent',
            'message_id': message['message_id'],
            'contact': contact_name,
            'message': message_body
        })

    except Exception as e:
        logger.error(f"Failed to send test message: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/logs', methods=['GET'])
def get_logs():
    """Get recent logs"""
    log_file = Path.home() / '.Adiyan' / 'orchestrator.log'
    if log_file.exists():
        with open(log_file, 'r') as f:
            lines = f.readlines()[-50:]  # Last 50 lines
            return jsonify({'logs': lines})
    return jsonify({'logs': []})

@app.route('/api/history', methods=['GET'])
def get_history():
    """Get interaction history"""
    limit = request.args.get('limit', 10, type=int)
    history_file = Path.home() / '.Adiyan' / 'interaction_history.jsonl'

    if history_file.exists():
        import json
        with open(history_file, 'r') as f:
            lines = f.readlines()
            records = [json.loads(line) for line in lines[-limit:]]
            return jsonify({'records': records})

    return jsonify({'records': []})

# ============= ERROR HANDLERS =============

_openwa_session_id_cache = None

def _resolve_openwa_session_id():
    """Resolve and cache the OpenWA session UUID for the configured session name."""
    global _openwa_session_id_cache
    if _openwa_session_id_cache:
        return _openwa_session_id_cache

    cfg = control_plane.config
    resp = requests.get(
        f"{cfg.openwa_url}/api/sessions",
        headers={'X-API-Key': cfg.openwa_api_key},
        timeout=5,
    )
    resp.raise_for_status()
    for s in resp.json():
        if s.get('name') == cfg.openwa_session_name:
            _openwa_session_id_cache = s['id']
            return s['id']
    raise RuntimeError(f"No OpenWA session named '{cfg.openwa_session_name}'")

def _openwa_session_status():
    cfg = control_plane.config
    session_id = _resolve_openwa_session_id()
    resp = requests.get(
        f"{cfg.openwa_url}/api/sessions/{session_id}",
        headers={'X-API-Key': cfg.openwa_api_key},
        timeout=5,
    )
    resp.raise_for_status()
    return resp.json()

@app.route('/api/whatsapp/status', methods=['GET'])
def whatsapp_status():
    """Check the OpenWA session's connection status"""
    try:
        session = _openwa_session_status()
        status = session.get('status')
        return jsonify({
            'connected': status == 'ready',
            'hasQR': status == 'qr_ready',
            'status': status,
            'phone': session.get('phone'),
        })
    except Exception as e:
        logger.warning(f"Failed to fetch OpenWA session status: {e}")
        return jsonify({'connected': False, 'hasQR': False, 'message': 'OpenWA not reachable'}), 503

@app.route('/api/whatsapp/qr/image', methods=['GET'])
def whatsapp_qr_image():
    """Fetch the current QR code from OpenWA, as a ready-to-display data URL"""
    try:
        cfg = control_plane.config
        session_id = _resolve_openwa_session_id()

        status_resp = requests.get(
            f"{cfg.openwa_url}/api/sessions/{session_id}",
            headers={'X-API-Key': cfg.openwa_api_key},
            timeout=5,
        )
        status_resp.raise_for_status()
        status = status_resp.json().get('status')

        if status == 'ready':
            return jsonify({'error': 'Already connected'}), 200
        if status != 'qr_ready':
            return jsonify({'error': f'QR not ready yet (status: {status})'}), 503

        qr_resp = requests.get(
            f"{cfg.openwa_url}/api/sessions/{session_id}/qr",
            headers={'X-API-Key': cfg.openwa_api_key},
            timeout=5,
        )
        qr_resp.raise_for_status()
        qr_code = qr_resp.json().get('qrCode')
        if not qr_code:
            return jsonify({'error': 'No QR available yet'}), 503

        return jsonify({'qr': qr_code})

    except requests.exceptions.ConnectionError:
        return jsonify({'error': f'OpenWA not reachable at {control_plane.config.openwa_url}'}), 503
    except Exception as e:
        logger.error(f"Failed to fetch QR image: {e}")
        return jsonify({'error': str(e)}), 500

@app.errorhandler(404)
def not_found(error):
    return jsonify({'error': 'Not found'}), 404

@app.errorhandler(500)
def server_error(error):
    return jsonify({'error': 'Server error', 'message': str(error)}), 500

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    logging.getLogger('werkzeug').setLevel(logging.WARNING)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    app.run(host='0.0.0.0', port=5000, debug=False)
