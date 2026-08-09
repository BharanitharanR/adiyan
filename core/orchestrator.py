from typing import List, Callable, Optional
from core.base_agent import BaseAgent, AgentState
from config.control_plane import ControlPlane, AGENT_CLASS_TO_KEY
import logging

logger = logging.getLogger('Orchestrator')

class Orchestrator:
    """Main orchestrator that chains all 7 agents"""

    def __init__(self, agents: List[BaseAgent], control_plane: ControlPlane = None):
        self.agents = agents
        self.control_plane = control_plane or ControlPlane()
        self.logger = logger

    async def execute_pipeline(self, state: AgentState) -> AgentState:
        """Execute all agents in sequence"""
        self.logger.info(f"[Pipeline] Starting orchestration for message from {state.contact_name}")

        for agent in self.agents:
            # Check if agent is enabled in control plane
            agent_config = self.control_plane.get_agent_config(AGENT_CLASS_TO_KEY.get(agent.name, agent.name))
            if agent_config and not agent_config.enabled:
                self.logger.info(f"[Pipeline] Skipping disabled agent: {agent.name}")
                continue

            # Execute agent
            try:
                state = await agent.execute(state)

                # Stop pipeline if error
                if state.error:
                    self.logger.error(f"[Pipeline] Stopping at {agent.name}: {state.error}")
                    break

            except Exception as e:
                state.error = f"{agent.name}: {str(e)}"
                self.logger.error(f"[Pipeline] Agent failed: {state.error}")
                break

        self.logger.info(f"[Pipeline] Complete for {state.contact_name}")
        return state

    def get_agent_status(self) -> dict:
        """Get status of all agents"""
        result = {}
        for agent in self.agents:
            cfg = self.control_plane.get_agent_config(AGENT_CLASS_TO_KEY.get(agent.name, agent.name))
            result[agent.name] = {
                'enabled': cfg.enabled if cfg else True,
                'tools': agent.tools
            }
        return result

    def update_agent_tools(self, agent_name: str, tools: List[str]) -> bool:
        """Update agent tools (called by UI)"""
        return self.control_plane.update_agent_tools(agent_name, tools)

    def toggle_agent(self, agent_name: str, enabled: bool) -> bool:
        """Enable/disable agent (called by UI)"""
        if enabled:
            return self.control_plane.enable_agent(agent_name)
        else:
            return self.control_plane.disable_agent(agent_name)
