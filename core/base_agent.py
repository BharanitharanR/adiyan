from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)

@dataclass
class AgentState:
    """State passed through the pipeline"""
    message_id: str
    contact_name: str
    lid: str
    message_body: str
    is_registration: bool = False
    is_unregistration: bool = False
    is_job_response: bool = False
    is_whitelisted: bool = False
    persona: Optional[str] = None
    llm_response: Optional[str] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}

class BaseAgent(ABC):
    """Base class for all agents in the pipeline"""

    def __init__(self, name: str, tools: List[str], config: Dict[str, Any] = None):
        self.name = name
        self.tools = tools
        self.config = config or {}
        self.logger = logging.getLogger(f"Agent.{name}")

    @abstractmethod
    async def execute(self, state: AgentState) -> AgentState:
        """
        Execute agent logic.
        Must return updated state for next agent.
        """
        pass

    def log_stage(self, message: str, level: str = 'info'):
        """Log with stage prefix"""
        log_msg = f"[{self.name}] {message}"
        if level == 'debug':
            self.logger.debug(log_msg)
        elif level == 'warning':
            self.logger.warning(log_msg)
        elif level == 'error':
            self.logger.error(log_msg)
        else:
            self.logger.info(log_msg)

    def has_error(self, state: AgentState) -> bool:
        """Check if state has error from previous agent"""
        return state.error is not None

    def set_error(self, state: AgentState, message: str) -> AgentState:
        """Set error and stop pipeline"""
        state.error = f"{self.name}: {message}"
        self.log_stage(message, 'error')
        return state
