#!/usr/bin/env python3
"""
Adiyan - WhatsApp Coaching Orchestrator
7-Stage LangGraph-based pipeline with UI control plane
"""

import asyncio
import logging
import sys
from pathlib import Path

# Setup paths
DATA_DIR = Path.home() / '.Adiyan'
DATA_DIR.mkdir(exist_ok=True)

# Configure logging
log_file = DATA_DIR / 'orchestrator.log'
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger('Adiyan')

# Dashboard polling (QR/status/agent list, every few seconds) is not a real
# request — don't let werkzeug's per-request access log drown out actual
# pipeline events.
logging.getLogger('werkzeug').setLevel(logging.WARNING)

# Same reasoning for the OpenWA poller's own outbound requests: httpx logs every
# GET it makes (every 3s to fetch recent messages) at INFO — that's the poller
# doing its job, not a noteworthy event. Actual failures still surface via
# OpenWAPoller's own error/backoff logging.
logging.getLogger('httpx').setLevel(logging.WARNING)

# Import components
from config.control_plane import ControlPlane, AGENT_CLASS_TO_KEY
from core.orchestrator import Orchestrator
from core.base_agent import AgentState
from agents.parser_agent import ParserAgent
from agents.validator_agent import ValidatorAgent
from agents.router_agent import RouterAgent
from agents.llm_agent import LLMAgent
from agents.synthesizer_agent import SynthesizerAgent
from agents.storage_agent import StorageAgent
from agents.publisher_agent import PublisherAgent
from services.whatsapp_bridge import WhatsAppBridge
from services.openwa_receiver import OpenWAReceiver
from services.openwa_service import OpenWAService
from services.openwa_poller import OpenWAPoller
from services.kb_ingestion_poller import KBIngestionPoller
from services.owner_admin_handler import OwnerAdminHandler
from services.qdrant_service import QdrantService
from core.mcp_tools import load_mcp_tools
from core.memory_index import get_memory_index
from ui.control_panel_api import app as flask_app
import threading
import pika
import json
import uuid

class AdiyanService:
    """Main Adiyan orchestrator service"""

    def __init__(self):
        self.control_plane = ControlPlane()
        self.orchestrator = None
        self.rabbitmq_connection = None
        self.channel = None
        self.whatsapp_bridge = WhatsAppBridge(
            rabbitmq_url=self.control_plane.config.rabbitmq_url
        )
        self.openwa_receiver = None
        self.openwa_service = None
        self.openwa_poller = None
        self.kb_poller = None
        self._openwa_loop = None
        self._openwa_poller_thread = None
        self._openwa_shutdown = threading.Event()
        self.qdrant_service = QdrantService()
        logger.info("✅ Adiyan service initialized")

    def setup_agents(self):
        """Create all 7 agents with configurations"""
        config_dict = self.control_plane.config.__dict__

        # Get agent-specific configs
        llm_config = self.control_plane.get_agent_config(AGENT_CLASS_TO_KEY['LLMAgent'])

        # MCP tools are loaded once, synchronously, before the pipeline starts
        # taking messages - loading is one-shot startup work, not per-message.
        mcp_tools = asyncio.run(load_mcp_tools())

        agents = [
            ParserAgent(config_dict),
            ValidatorAgent(config_dict),
            RouterAgent(config_dict),
            LLMAgent(config_dict, agent_config=llm_config, mcp_tools=mcp_tools, control_plane=self.control_plane),
            SynthesizerAgent(config_dict),
            StorageAgent(config_dict),
            PublisherAgent(config_dict, whatsapp_sender=self._send_via_openwa)
        ]

        self.orchestrator = Orchestrator(agents, self.control_plane)
        logger.info("✅ All 7 agents initialized")
        for agent in agents:
            agent_cfg = self.control_plane.get_agent_config(AGENT_CLASS_TO_KEY.get(agent.name, agent.name))
            if agent_cfg:
                logger.info(f"   {agent.name}: model={agent_cfg.model}, temp={agent_cfg.temperature}")
        return agents

    def setup_rabbitmq(self):
        """Connect to RabbitMQ and setup queues"""
        try:
            connection_url = self.control_plane.config.rabbitmq_url
            self.rabbitmq_connection = pika.BlockingConnection(
                pika.URLParameters(connection_url)
            )
            self.channel = self.rabbitmq_connection.channel()

            # Declare exchange
            self.channel.exchange_declare(
                exchange='adiyan',
                exchange_type='topic',
                durable=True
            )

            # Declare input queue for messages
            self.channel.queue_declare(queue='messages', durable=True)
            self.channel.queue_bind(
                exchange='adiyan',
                queue='messages',
                routing_key='messages.incoming'
            )

            logger.info(f"✅ Connected to RabbitMQ: {connection_url}")
            return True

        except Exception as e:
            logger.warning(f"⚠️  RabbitMQ connection failed: {e}")
            logger.warning("   Running without RabbitMQ (messages won't queue)")
            return False

    def start_message_consumer(self):
        """Listen for messages on RabbitMQ"""
        if not self.channel:
            logger.warning("⚠️  RabbitMQ not available - skipping message consumer")
            return

        def callback(ch, method, properties, body):
            try:
                message_data = json.loads(body)
                logger.info(f"📬 Received message from {message_data.get('contact_name')}")

                # Run orchestrator
                asyncio.run(self._process_message(message_data))

                # Acknowledge
                ch.basic_ack(delivery_tag=method.delivery_tag)

            except Exception as e:
                logger.error(f"❌ Message processing failed: {e}")
                ch.basic_nack(delivery_tag=method.delivery_tag)

        # Start consuming in background thread
        def consume():
            try:
                self.channel.basic_qos(prefetch_count=1)
                self.channel.basic_consume(
                    queue='messages',
                    on_message_callback=callback
                )
                logger.info("👂 Listening for messages...")
                self.channel.start_consuming()
            except Exception as e:
                logger.error(f"Consumer error: {e}")

        thread = threading.Thread(target=consume, daemon=True)
        thread.start()

    async def _process_message(self, message_data: dict):
        """Process a single message through the pipeline"""
        try:
            # Create state
            state = AgentState(
                message_id=message_data.get('message_id', str(uuid.uuid4())),
                contact_name=message_data.get('contact_name', 'Unknown'),
                lid=message_data.get('lid', 'unknown'),
                message_body=message_data.get('message_body', '')
            )

            # Mark if this is a user message (prevent reprocessing responses)
            state.metadata['is_user_message'] = message_data.get('is_user_message', True)

            # Execute pipeline
            result = await self.orchestrator.execute_pipeline(state)

            if result.error:
                logger.error(f"❌ Pipeline error: {result.error}")
            else:
                logger.info(f"✅ Message processed for {result.contact_name}")

        except Exception as e:
            logger.error(f"❌ Message processing error: {e}")

    def start_whatsapp_bridge(self):
        """Start WhatsApp bridge"""
        if not self.whatsapp_bridge.setup_rabbitmq():
            logger.warning("⚠️  WhatsApp bridge RabbitMQ connection failed")
            return

        self.whatsapp_bridge.setup_whatsapp()

        # Start reply listener in background
        def listen():
            self.whatsapp_bridge.listen_for_replies()

        thread = threading.Thread(target=listen, daemon=True)
        thread.start()
        logger.info("✅ WhatsApp bridge started")

    def setup_openwa_receiver(self):
        """Setup OpenWA webhook receiver"""
        try:
            # Create receiver with orchestrator callback
            self.openwa_receiver = OpenWAReceiver(
                orchestrator_callback=self._process_message
            )
            logger.info("✅ OpenWA Receiver initialized")

            # Make available to Flask
            flask_app.config['OPENWA_RECEIVER'] = self.openwa_receiver

        except Exception as e:
            logger.error(f"❌ Failed to setup OpenWA receiver: {e}")

    def setup_openwa_service(self):
        """Create the OpenWA HTTP client - used both to send replies and by the poller"""
        cfg = self.control_plane.config
        try:
            self.openwa_service = OpenWAService(
                base_url=cfg.openwa_url,
                api_key=cfg.openwa_api_key,
                session_name=cfg.openwa_session_name,
            )
            logger.info(f"✅ OpenWA service configured for session '{cfg.openwa_session_name}'")
        except Exception as e:
            logger.error(f"❌ Failed to configure OpenWA service: {e}")

    async def _send_via_openwa(self, lid: str, response: str):
        """PublisherAgent's whatsapp_sender - sends a reply through OpenWA"""
        if not self.openwa_service:
            raise RuntimeError("OpenWA service not initialized")
        await self.openwa_service.send_message(lid, response)

    def setup_openwa_poller(self):
        """Configure the OpenWA poller (webhooks are blocked for localhost destinations,
        so OpenWA is polled for new messages instead of pushing them to us)"""
        if not self.openwa_service:
            logger.warning("⚠️  OpenWA service not configured - skipping poller setup")
            return

        cfg = self.control_plane.config
        self.openwa_poller = OpenWAPoller(
            openwa_service=self.openwa_service,
            orchestrator_callback=self._process_message,
            poll_interval_seconds=cfg.openwa_poll_interval_seconds,
        )
        logger.info("✅ OpenWA poller configured")

        memory_index = get_memory_index(cfg.qdrant_url, cfg.ollama_url)
        if memory_index:
            admin_handler = OwnerAdminHandler(self.control_plane, self.openwa_service, ollama_url=cfg.ollama_url)
            self.kb_poller = KBIngestionPoller(
                openwa_service=self.openwa_service,
                memory_index=memory_index,
                admin_handler=admin_handler,
            )
            logger.info("✅ KB ingestion poller + WhatsApp admin handler configured")
        else:
            logger.warning("⚠️  Memory index unavailable - skipping KB ingestion poller")

    def start_openwa_poller(self):
        """Run the OpenWA poller in its own thread with its own asyncio event loop,
        so it doesn't block (or depend on) the RabbitMQ/Flask threads."""
        if not self.openwa_poller:
            logger.warning("⚠️  OpenWA poller not configured - skipping start")
            return

        async def start_with_retry():
            # The OpenWA Node service may not be up yet when Adiyan starts (or may
            # bounce later) - a connection failure here must not permanently kill
            # the poller for the rest of the process's life.
            delay = self.control_plane.config.openwa_poll_interval_seconds
            last_error_signature = None
            attempt = 0
            while not self._openwa_shutdown.is_set():
                try:
                    await self.openwa_poller.start()
                    if self.kb_poller:
                        await self.kb_poller.start()
                    if attempt:
                        logger.info(f"✅ OpenWA poller connected after {attempt} failed attempt(s)")
                    return True
                except Exception as e:
                    attempt += 1
                    signature = str(e)
                    if signature != last_error_signature:
                        logger.error(f"❌ OpenWA poller failed to start (attempt {attempt}): {e}")
                        last_error_signature = signature
                    delay = min(60.0, delay * 2) if attempt > 1 else delay
                    await asyncio.sleep(delay)
            return False

        def run_loop():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._openwa_loop = loop
            try:
                started = loop.run_until_complete(start_with_retry())
                if started:
                    loop.run_forever()
            except Exception as e:
                logger.error(f"❌ OpenWA poller thread error: {e}")
            finally:
                try:
                    if self.kb_poller:
                        loop.run_until_complete(self.kb_poller.stop())
                    loop.run_until_complete(self.openwa_poller.stop())
                    loop.run_until_complete(self.openwa_service.close())
                except Exception as e:
                    logger.warning(f"⚠️  OpenWA poller cleanup error: {e}")
                loop.close()

        thread = threading.Thread(target=run_loop, daemon=True)
        thread.start()
        self._openwa_poller_thread = thread
        logger.info("🚀 OpenWA poller thread started")

    def start_ui_server(self, port=5001):
        """Start Flask control panel UI"""
        # Make services available to Flask
        flask_app.config['WHATSAPP_BRIDGE'] = self.whatsapp_bridge
        flask_app.config['OPENWA_RECEIVER'] = self.openwa_receiver

        def run_flask():
            logger.info(f"🌐 Control Panel UI: http://localhost:{port}")
            logger.info("📊 Dashboard: http://localhost:{port}".format(port=port))
            flask_app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

        thread = threading.Thread(target=run_flask, daemon=True)
        thread.start()

    def run(self):
        """Start the service"""
        logger.info("=" * 60)
        logger.info("🚀 Adiyan Orchestrator Service Starting")
        logger.info("=" * 60)

        # Setup
        try:
            asyncio.run(self.qdrant_service.start())
        except Exception as e:
            logger.error(f"❌ Bundled Qdrant failed to start: {e} - continuing without memory recall")

        self.setup_openwa_service()
        self.setup_agents()
        self.setup_rabbitmq()
        self.setup_openwa_receiver()
        self.setup_openwa_poller()
        self.start_ui_server(port=5001)
        self.start_whatsapp_bridge()
        self.start_message_consumer()
        self.start_openwa_poller()

        # Print status
        logger.info("")
        logger.info("📊 Pipeline Configuration:")
        for name, config in self.control_plane.get_all_configs().items():
            status = "✓ ENABLED" if config.enabled else "○ DISABLED"
            logger.info(f"   {status} - {config.name}")

        logger.info("")
        logger.info("Ready for messages!")
        logger.info("Send messages via: curl -X POST http://localhost:8000/process -d '{...}'")
        logger.info("")

        # Keep running
        try:
            import time
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("🛑 Shutting down...")
            if self.rabbitmq_connection:
                self.rabbitmq_connection.close()
            self._openwa_shutdown.set()
            if self.openwa_poller and self._openwa_loop:
                self._openwa_loop.call_soon_threadsafe(self._openwa_loop.stop)
                if self._openwa_poller_thread:
                    self._openwa_poller_thread.join(timeout=5)
            self.qdrant_service.stop()

if __name__ == '__main__':
    service = AdiyanService()
    service.run()
