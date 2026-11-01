"""
Meta Model API chat client - a LangChain ChatOpenAI pointed at Meta's own
OpenAI-compatible endpoint via a custom base_url. base_url/api_key/model are
read from config_sdk/secrets_vault here, the same pattern claude_cli.py's
model selection already uses - so pointing an agent's calls at Meta is a
config change (set provider='meta' on that stage, see agent_sdk.py's ask()),
never a code change.

Built on langchain_openai.ChatOpenAI deliberately, not a raw httpx client, so
mesh/observability/tracing.py's auto_instrument=True picks up every call for
free, the same way the existing ChatOllama path already does. claude_cli.py
can't get this (it shells out to a subprocess, no LangChain object exists);
this path can, because it's a real LangChain chat model start to finish.

MODEL_API_KEY lives in the OS keychain (mesh/lib/secrets_vault.py) - set it
once via mesh/tools/set_secret.py, never hardcoded here or anywhere else.
"""
import logging
from typing import Any, List, Optional, Type

from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from mesh.lib import config_sdk
from mesh.lib.secrets_vault import get_secret

logger = logging.getLogger('MetaModelClient')

# Pseudo agent_id for this provider's own dashboard-editable constants -
# same shape as agent_sdk.py's _BACKEND_AGENT_ID ('_llm_backend').
_AGENT_ID = '_meta_model'

DEFAULT_BASE_URL = 'https://api.meta.ai/v1'
# dev.meta.ai/docs/models names muse-spark-1.3 as the current default;
# muse-spark-1.1 is called out as still working - config-overridable so a
# model-id change never needs a code deploy.
DEFAULT_MODEL = 'muse-spark-1.3'


class MetaModelError(RuntimeError):
    pass


async def _client(model: str, temperature: float) -> ChatOpenAI:
    api_key = get_secret('MODEL_API_KEY')
    if not api_key:
        raise MetaModelError(
            "MODEL_API_KEY is not set in the vault - run mesh/tools/set_secret.py to add it "
            "before any stage can use provider='meta'."
        )
    base_url = await config_sdk.get_constant(
        _AGENT_ID, 'base_url', DEFAULT_BASE_URL,
        description="Meta Model API base URL (OpenAI SDK-compatible).",
    )
    return ChatOpenAI(base_url=base_url, api_key=api_key, model=model, temperature=temperature)


async def run(
    prompt: str, *, model: str = DEFAULT_MODEL, temperature: float = 0.4,
    schema: Optional[Type[BaseModel]] = None, tools: Optional[List[Any]] = None,
) -> Any:
    """Mirrors agent_sdk.py's ask() call shapes (plain-text / schema / tool-
    selection) for the one branch that routes here. Never raises anything
    but MetaModelError - ask() catches it and falls through to local Ollama,
    the same "unavailable backend degrades a call, never breaks it" contract
    claude_cli.py's run() already keeps.

    tools: real LangChain @tool-decorated callables, bound with
    .bind_tools() same as the ChatOllama path - Muse Spark's native tool
    calling (including parallel calls) is documented OpenAI-Chat-Completions-
    shaped, so this is expected to work unchanged through ChatOpenAI.
    Returns the AIMessage (caller reads .tool_calls), not just text.

    NOT wired here: Muse Spark's built-in web_search tool
    ({"type": "web_search"}). That tool is documented on the Responses API,
    not Chat Completions - langchain_openai's ChatOpenAI supports the
    Responses API via use_responses_api=True, but whether Meta's endpoint
    actually serves /v1/responses (vs. only /v1/chat/completions) is
    unverified; there is no API key in this environment to test against.
    Flagged for the next step that touches web search, not guessed at here."""
    llm = await _client(model, temperature)
    try:
        if tools is not None:
            result = await llm.bind_tools(tools).ainvoke(prompt)
            return result
        if schema is not None:
            return await llm.with_structured_output(schema).ainvoke(prompt)
        result = await llm.ainvoke(prompt)
        return result.content
    except Exception as e:
        raise MetaModelError(str(e)) from e
