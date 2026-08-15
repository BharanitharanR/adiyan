from dataclasses import asdict
from typing import List
from langgraph.graph import StateGraph, END
from core.base_agent import BaseAgent, AgentState
from config.control_plane import ControlPlane, AGENT_CLASS_TO_KEY
import logging

logger = logging.getLogger('Orchestrator')

class Orchestrator:
    """Main orchestrator that chains all 7 agents, as a compiled LangGraph StateGraph"""

    def __init__(self, agents: List[BaseAgent], control_plane: ControlPlane = None):
        self.agents = agents
        self.control_plane = control_plane or ControlPlane()
        self.logger = logger
        self._graph = self._build_graph()

    def _build_graph(self):
        """Wire the agents into a single linear chain (same order/semantics as the
        old for-loop): a disabled agent or one that already set state.error becomes
        a no-op rather than actually halting the graph, so the observable result -
        stop making progress once something's wrong - matches the old `break`."""
        graph = StateGraph(AgentState)

        for agent in self.agents:
            graph.add_node(agent.name, self._make_node(agent))

        node_names = [agent.name for agent in self.agents]
        graph.set_entry_point(node_names[0])
        for current_name, next_name in zip(node_names, node_names[1:]):
            graph.add_edge(current_name, next_name)
        graph.add_edge(node_names[-1], END)

        return graph.compile()

    def _make_node(self, agent: BaseAgent):
        async def node(state: AgentState):
            if state.error:
                return {}

            agent_config = self.control_plane.get_agent_config(AGENT_CLASS_TO_KEY.get(agent.name, agent.name))
            if agent_config and not agent_config.enabled:
                self.logger.info(f"[Pipeline] Skipping disabled agent: {agent.name}")
                return {}

            try:
                updated = await agent.execute(state)
            except Exception as e:
                state.error = f"{agent.name}: {str(e)}"
                self.logger.error(f"[Pipeline] Agent failed: {state.error}")
                return asdict(state)

            if updated.error:
                self.logger.error(f"[Pipeline] Stopping at {agent.name}: {updated.error}")
            return asdict(updated)
        return node

    async def execute_pipeline(self, state: AgentState) -> AgentState:
        """Execute all agents in sequence"""
        self.logger.info(f"[Pipeline] Starting orchestration for message from {state.contact_name}")

        result_dict = await self._graph.ainvoke(asdict(state))
        result = AgentState(**result_dict)

        self.logger.info(f"[Pipeline] Complete for {result.contact_name}")
        return result

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
