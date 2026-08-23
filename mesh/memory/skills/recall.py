"""
recall_contact_memory's real body - a thin, honest wrapper around
mesh/memory/mem0_backend.py's retrieve(). No judgment here about what the
snippets mean or what to do with them - that's the caller's job (Journal
Agent). This just fetches.

The only place under mesh/ that reaches into the conversation-memory stack
directly - the whole reason Memory Agent exists as its own agent instead of
being a dependency baked into Journal Agent.
"""
from typing import Any, Dict

from mesh.memory import mem0_backend


def run(contact_name: str, query: str, top_k: int = 3) -> Dict[str, Any]:
    # int(...): a structured DataPart call skips Pydantic coercion (see
    # agent_executor.py's execute() - only the free-text classify/extract
    # path validates through RecallParams) and travels through A2A's
    # protobuf Struct, which has no integer type, only double - confirmed
    # live the same way handle_message.py's 'chunks' field once was: a real
    # int top_k sent by a caller (Analysis Agent's recall_memory tool)
    # silently became 5.0 by the time it got here, and mem0's own search()
    # rejects a non-int top_k outright ("top_k must be a valid integer"),
    # unlike the chunks case which just printed wrong.
    snippets = mem0_backend.retrieve(contact_name=contact_name, query=query, top_k=int(top_k))
    return {'snippets': snippets, 'available': mem0_backend.is_available()}
