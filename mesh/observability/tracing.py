"""
Shared LLM tracing setup for every agent under platform/.

One Phoenix collector (the full `arize-phoenix` package, run separately via
`phoenix serve` - not a dependency of any individual agent) receives spans
from every agent. This file is the one place the collector's endpoint is
declared, so changing it never means editing every agent's own config.

Each agent that wants its LLM calls traced needs only the lightweight client
side installed:
    pip install openinference-instrumentation-langchain arize-phoenix-otel
and calls setup_tracing(project_name=<this agent's id>) once at startup,
before any LangChain call it wants captured. auto_instrument=True means no
manual span code is needed at each call site - LangChain/LangGraph calls are
captured automatically once this has run.
"""
import json
from pathlib import Path

from phoenix.otel import register

CONFIG_PATH = Path(__file__).parent / 'config.json'


def setup_tracing(project_name: str):
    """Registers this agent's traces with the shared Phoenix collector.

    project_name should be this agent's own id (e.g. 'scheduler') so its
    spans are distinguishable from every other agent's in the shared
    collector, rather than all agents' traces blurring together under one
    name.
    """
    config = json.loads(CONFIG_PATH.read_text())
    return register(
        project_name=project_name,
        endpoint=config['collector_endpoint'],
        protocol=config.get('protocol'),
        auto_instrument=True,
        batch=config.get('batch', True),
    )
