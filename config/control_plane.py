from dataclasses import dataclass, asdict
from typing import Dict, List, Any, Optional
import json
import os
from pathlib import Path

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

@dataclass
class AgentConfig:
    """Configuration for each agent"""
    name: str
    enabled: bool
    tools: List[str]
    model: str = "qwen3:8b-16k"
    temperature: float = 0.7
    timeout: int = 60
    retry_count: int = 3
    custom_params: Dict[str, Any] = None

    def __post_init__(self):
        if self.custom_params is None:
            self.custom_params = {}

@dataclass
class PipelineConfig:
    """Central control plane configuration"""
    agents: Dict[str, AgentConfig]
    ollama_url: str = "http://localhost:11434"
    qdrant_url: str = "http://localhost:6333"
    rabbitmq_url: str = "amqp://guest:guest@localhost/"
    whitelist_enabled: bool = True
    whitelist_prefix: str = "USER"
    max_response_length: int = 4096
    openwa_url: str = "http://localhost:2785"
    openwa_api_key: str = ""
    openwa_session_name: str = "executive-coach"
    openwa_poll_interval_seconds: float = 3.0

    def to_dict(self):
        return {
            'agents': {name: asdict(cfg) for name, cfg in self.agents.items()},
            'ollama_url': self.ollama_url,
            'qdrant_url': self.qdrant_url,
            'rabbitmq_url': self.rabbitmq_url,
            'whitelist_enabled': self.whitelist_enabled,
            'whitelist_prefix': self.whitelist_prefix,
            'max_response_length': self.max_response_length,
            'openwa_url': self.openwa_url,
            'openwa_api_key': self.openwa_api_key,
            'openwa_session_name': self.openwa_session_name,
            'openwa_poll_interval_seconds': self.openwa_poll_interval_seconds
        }

class ControlPlane:
    """Central control plane - manages all agent configurations"""

    def __init__(self, config_file: str = None):
        if config_file is None:
            config_file = str(DATA_DIR / 'pipeline.json')
        self.config_file = config_file
        self.config = self._load_or_create_config()

    def _load_or_create_config(self) -> PipelineConfig:
        """Load config from file or create default"""
        if os.path.exists(self.config_file):
            with open(self.config_file, 'r') as f:
                data = json.load(f)
                return self._dict_to_config(data)

        # Default config with 7 agents
        return self._create_default_config()

    @staticmethod
    def _default_agents() -> Dict[str, AgentConfig]:
        """The canonical 7-agent defaults - the only place this schema is defined."""
        return {
            'parser': AgentConfig(
                name='Parser Agent',
                enabled=True,
                tools=['extract_message', 'get_contact_info', 'parse_lid']
            ),
            'validator': AgentConfig(
                name='Validator Agent',
                enabled=True,
                tools=['check_whitelist', 'validate_format', 'check_registration']
            ),
            'router': AgentConfig(
                name='Router Agent',
                enabled=True,
                tools=['load_persona', 'get_routing_rules', 'determine_flow']
            ),
            'llm': AgentConfig(
                name='LLM Agent',
                enabled=True,
                tools=['call_ollama', 'get_context', 'apply_system_prompt'],
                temperature=0.7
            ),
            'synthesizer': AgentConfig(
                name='Synthesizer Agent',
                enabled=True,
                tools=['format_response', 'split_chunks', 'apply_persona_rules']
            ),
            'storage': AgentConfig(
                name='Storage Agent',
                enabled=True,
                tools=['store_in_qdrant', 'save_to_file', 'update_metadata']
            ),
            'publisher': AgentConfig(
                name='Publisher Agent',
                enabled=True,
                tools=['send_whatsapp_reply', 'publish_event', 'log_completion']
            )
        }

    def _create_default_config(self) -> PipelineConfig:
        """Create default 7-agent configuration"""
        config = PipelineConfig(agents=self._default_agents())
        self.save_config(config)
        return config

    def _dict_to_config(self, data: Dict) -> PipelineConfig:
        """Convert dict to PipelineConfig.

        Agent entries are merged over the defaults rather than required to be complete: a
        pre-seeded file (e.g. the installer writing just {"agents": {"llm": {"model": "..."}}}
        before this ever runs) is a valid partial config, not a broken one - only the fields it
        actually specifies should override the default for that agent.
        """
        defaults = self._default_agents()
        agents = dict(defaults)
        for name, agent_data in data.get('agents', {}).items():
            base = asdict(defaults[name]) if name in defaults else {}
            agents[name] = AgentConfig(**{**base, **agent_data})

        return PipelineConfig(
            agents=agents,
            ollama_url=data.get('ollama_url', 'http://localhost:11434'),
            qdrant_url=data.get('qdrant_url', 'http://localhost:6333'),
            rabbitmq_url=data.get('rabbitmq_url', 'amqp://guest:guest@localhost/'),
            whitelist_enabled=data.get('whitelist_enabled', True),
            whitelist_prefix=data.get('whitelist_prefix', 'USER'),
            max_response_length=data.get('max_response_length', 4096),
            openwa_url=data.get('openwa_url', 'http://localhost:2785'),
            openwa_api_key=data.get('openwa_api_key', ''),
            openwa_session_name=data.get('openwa_session_name', 'executive-coach'),
            openwa_poll_interval_seconds=data.get('openwa_poll_interval_seconds', 3.0)
        )

    def save_config(self, config: Optional[PipelineConfig] = None):
        """Save current config to file"""
        cfg = config or self.config
        os.makedirs(os.path.dirname(self.config_file), exist_ok=True)
        with open(self.config_file, 'w') as f:
            json.dump(cfg.to_dict(), f, indent=2)

    def update_agent_tools(self, agent_name: str, tools: List[str]):
        """Update agent tools (used by UI)"""
        if agent_name in self.config.agents:
            self.config.agents[agent_name].tools = tools
            self.save_config()
            return True
        return False

    def enable_agent(self, agent_name: str):
        """Enable agent"""
        if agent_name in self.config.agents:
            self.config.agents[agent_name].enabled = True
            self.save_config()
            return True
        return False

    def disable_agent(self, agent_name: str):
        """Disable agent"""
        if agent_name in self.config.agents:
            self.config.agents[agent_name].enabled = False
            self.save_config()
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
            self.save_config()
            return True
        return False
