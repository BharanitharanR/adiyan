from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
from config.control_plane import ControlPlane
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

PERSONAS_FILE = Path.home() / '.Adiyan' / 'personas.json'

def _load_personas_file() -> dict:
    """Read personas.json as-is. RouterAgent seeds this file with defaults on its
    first pipeline run, but the dashboard may be opened before that has happened."""
    if not PERSONAS_FILE.exists():
        return {'active_persona': None, 'personas': {}}
    with open(PERSONAS_FILE, 'r') as f:
        return json.load(f)

def _save_personas_file(data: dict):
    """Write personas.json atomically so RouterAgent's mtime-based poll (which runs
    on every pipeline message) never sees a partially-written file."""
    tmp_path = PERSONAS_FILE.with_suffix('.json.tmp')
    with open(tmp_path, 'w') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, PERSONAS_FILE)

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
            'enabled': config.enabled,
            'tools': config.tools,
            'model': config.model,
            'temperature': config.temperature,
            'timeout': config.timeout
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
        'enabled': config.enabled,
        'tools': config.tools,
        'model': config.model,
        'temperature': config.temperature,
        'timeout': config.timeout,
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
    """Update agent model configuration"""
    data = request.json
    model = data.get('model')
    temperature = data.get('temperature', 0.7)
    timeout = data.get('timeout', 60)

    config = control_plane.get_agent_config(agent_name)
    if config:
        config.model = model
        config.temperature = temperature
        config.timeout = timeout
        control_plane.save_config()
        return jsonify({
            'status': 'updated',
            'agent': agent_name,
            'model': model,
            'temperature': temperature,
            'timeout': timeout
        })
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
