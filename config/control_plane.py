from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional
import json
import os
from pathlib import Path

import config.database as db

# Default data directory
DATA_DIR = Path.home() / '.Adiyan'
DATA_DIR.mkdir(exist_ok=True)

# PipelineConfig.agents is keyed by these short lowercase names, but every
# BaseAgent instance identifies itself by its class-style name (agent.name,
# e.g. 'LLMAgent') - callers that look up config by agent.name need this map
# rather than using agent.name as the dict key directly.
AGENT_CLASS_TO_KEY = {
    'ParserAgent': 'parser',
    'ValidatorAgent': 'validator',
    'RouterAgent': 'router',
    'LLMAgent': 'llm',
    'SynthesizerAgent': 'synthesizer',
    'StorageAgent': 'storage',
    'PublisherAgent': 'publisher',
}

# The 6 reasoning-cycle agents living inside LLMAgent (config/database.py's
# REASONING_CYCLE_DEFAULTS is the canonical definition; kept here too since callers
# that want "all 13 ids" shouldn't have to reach into the db module directly).
REASONING_CYCLE_AGENT_IDS = ['hermes', 'prometheus', 'pythia', 'hephaestus', 'calliope', 'momus']


@dataclass
class AgentConfig:
    """Configuration for each agent (pipeline or reasoning-cycle)."""
    name: str
    enabled: bool
    tools: List[str]
    kind: str = 'pipeline'  # 'pipeline' or 'llm_stage'
    model: Optional[str] = None
    temperature: Optional[float] = None
    timeout: Optional[int] = None
    prompt_template: Optional[str] = None
    retry_count: int = 3
    custom_params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PipelineConfig:
    """Central control plane configuration"""
    agents: Dict[str, AgentConfig]
    ollama_url: str = "http://localhost:11434"
    # Adiyan's own bundled Qdrant (services/qdrant_service.py), not the shared default
    # 6333 - avoids colliding with any other Qdrant instance already on the machine.
    qdrant_url: str = "http://localhost:6339"
    rabbitmq_url: str = "amqp://guest:guest@localhost/"
    whitelist_enabled: bool = True
    whitelist_prefix: str = "USER"
    max_response_length: int = 4096
    openwa_url: str = "http://localhost:2785"
    openwa_api_key: str = ""
    openwa_session_name: str = "executive-coach"
    # 3s was tripping OpenWA's own 1000-req/hour local rate limit on its own (1200
    # req/hr just from this poller) - confirmed live, recurring roughly hourly.
    # 30s keeps Adiyan comfortably under that ceiling even with the KB poller and
    # normal admin/debug traffic added on top, at the cost of up to a 30s delay
    # before Adiyan notices a new incoming message.
    openwa_poll_interval_seconds: float = 30.0

    def to_dict(self):
        return {
            'agents': {name: vars(cfg) for name, cfg in self.agents.items()},
            'ollama_url': self.ollama_url,
            'qdrant_url': self.qdrant_url,
            'rabbitmq_url': self.rabbitmq_url,
            'whitelist_enabled': self.whitelist_enabled,
            'whitelist_prefix': self.whitelist_prefix,
            'max_response_length': self.max_response_length,
            'openwa_url': self.openwa_url,
            # Never the real value - this dict is exposed verbatim over
            # /api/config; a caller only ever needs to know whether it's set.
            'openwa_api_key': '***configured***' if self.openwa_api_key else '',
            'openwa_session_name': self.openwa_session_name,
            'openwa_poll_interval_seconds': self.openwa_poll_interval_seconds
        }


_SETTINGS_DEFAULTS = {
    'ollama_url': "http://localhost:11434",
    'qdrant_url': "http://localhost:6339",
    'rabbitmq_url': "amqp://guest:guest@localhost/",
    'whitelist_enabled': True,
    'whitelist_prefix': "USER",
    'max_response_length': 4096,
    'openwa_url': "http://localhost:2785",
    'openwa_api_key': "",
    'openwa_session_name': "executive-coach",
    'openwa_poll_interval_seconds': 30.0,
}


class ControlPlane:
    """Central control plane - manages all agent configurations.

    Backed by config/database.py (SQLite) rather than a flat pipeline.json. Loads the
    full state into self.config once at startup (same in-memory dataclass shape as
    before, so existing callers - core/orchestrator.py, ui/control_panel_api.py - don't
    need to change how they read it, including code that mutates an AgentConfig in
    place and then calls save_config()); every mutating method here writes straight
    through to the db so a fresh ControlPlane in another thread (e.g. the WhatsApp
    admin handler) sees the same data.
    """

    def __init__(self, legacy_json_path: str = None):
        db.init_db()
        self._migrate_legacy_files_if_present(legacy_json_path)
        self.config = self._load_config_from_db()

    def _migrate_legacy_files_if_present(self, legacy_json_path: str = None):
        """One-time import from the old pipeline.json/personas.json/whitelist.txt.
        Gated on db.has_migrated_from_files() - migration is a one-time import, not a
        sync; running it again on every startup would overwrite live db changes (a
        dashboard edit, a WhatsApp admin change) with whatever's still in the old files."""
        if db.has_migrated_from_files():
            return

        pipeline_json = None
        pipeline_path = Path(legacy_json_path) if legacy_json_path else (DATA_DIR / 'pipeline.json')
        if pipeline_path.exists():
            try:
                with open(pipeline_path, 'r') as f:
                    pipeline_json = json.load(f)
            except Exception:
                pipeline_json = None

        personas_json = None
        personas_path = DATA_DIR / 'personas.json'
        if personas_path.exists():
            try:
                with open(personas_path, 'r') as f:
                    personas_json = json.load(f)
            except Exception:
                personas_json = None

        whitelist_names = []
        whitelist_path = DATA_DIR / 'whitelist.txt'
        if whitelist_path.exists():
            try:
                with open(whitelist_path, 'r') as f:
                    whitelist_names = [line.strip() for line in f if line.strip()]
            except Exception:
                whitelist_names = []

        if pipeline_json or personas_json or whitelist_names:
            db.migrate_from_files(pipeline_json, personas_json, whitelist_names)

    def _load_config_from_db(self) -> PipelineConfig:
        agents = {}
        for agent_id, row in db.get_all_agent_configs().items():
            agents[agent_id] = AgentConfig(
                name=row['name'],
                enabled=row['enabled'],
                tools=row['tools'],
                kind=row['kind'],
                model=row['model'],
                temperature=row['temperature'],
                timeout=row['timeout'],
                prompt_template=row['prompt_template'],
                retry_count=row['retry_count'],
                custom_params=row['custom_params'],
            )

        settings = db.get_all_settings()
        kwargs = {k: settings.get(k, default) for k, default in _SETTINGS_DEFAULTS.items()}

        # The vault (OS Keychain, config/secrets_vault.py) is the source of truth
        # for this secret when set - wins over whatever plaintext value is still
        # sitting in the settings table from before the vault existed. Falling
        # back to the db value (not blanking it) means an un-migrated install
        # keeps working exactly as before until the owner runs tools/set_secret.py.
        from config.secrets_vault import get_secret
        vault_key = get_secret('OPENWA_API_KEY')
        if vault_key:
            kwargs['openwa_api_key'] = vault_key

        return PipelineConfig(agents=agents, **kwargs)

    def save_config(self, config: Optional[PipelineConfig] = None):
        """Persist the full current state (or the given one) back to the db - every
        agent row and every top-level setting. Kept as a full-state write (matching the
        old file-based save_config's semantics) since some callers still mutate an
        AgentConfig in place and then call this rather than going through a setter."""
        cfg = config or self.config
        for agent_id, agent_cfg in cfg.agents.items():
            db.update_agent_config(
                agent_id,
                enabled=agent_cfg.enabled,
                model=agent_cfg.model,
                temperature=agent_cfg.temperature,
                timeout=agent_cfg.timeout,
                prompt_template=agent_cfg.prompt_template,
                tools=agent_cfg.tools,
                retry_count=agent_cfg.retry_count,
            )
        for key in _SETTINGS_DEFAULTS:
            db.set_setting(key, getattr(cfg, key))

    def update_agent_tools(self, agent_name: str, tools: List[str]):
        """Update agent tools (used by UI)"""
        if agent_name in self.config.agents:
            self.config.agents[agent_name].tools = tools
            db.update_agent_config(agent_name, tools=tools)
            return True
        return False

    def update_agent_config(self, agent_name: str, **fields) -> bool:
        """Update any subset of an agent's model/temperature/timeout/prompt_template/
        enabled - the generalized setter the reasoning-cycle agents and the WhatsApp
        admin handler use, rather than mutate-then-save_config()."""
        if agent_name not in self.config.agents:
            return False
        agent_cfg = self.config.agents[agent_name]
        for key, value in fields.items():
            if hasattr(agent_cfg, key):
                setattr(agent_cfg, key, value)
        return db.update_agent_config(agent_name, **fields)

    def enable_agent(self, agent_name: str):
        """Enable agent"""
        if agent_name in self.config.agents:
            self.config.agents[agent_name].enabled = True
            db.set_agent_enabled(agent_name, True)
            return True
        return False

    def disable_agent(self, agent_name: str):
        """Disable agent"""
        if agent_name in self.config.agents:
            self.config.agents[agent_name].enabled = False
            db.set_agent_enabled(agent_name, False)
            return True
        return False

    def get_agent_config(self, agent_name: str) -> Optional[AgentConfig]:
        """Get single agent config"""
        return self.config.agents.get(agent_name)

    def get_all_configs(self) -> Dict[str, AgentConfig]:
        """Get all agent configs"""
        return self.config.agents

    def update_system_setting(self, key: str, value: Any):
        """Update system-level settings"""
        if hasattr(self.config, key):
            setattr(self.config, key, value)
            db.set_setting(key, value)
            return True
        return False
