"""Real semantic memory over past coaching interactions.

Replaces the old placeholder-vector write StorageAgent used to do (a constant
[0.1]*384 vector, into a collection nothing ever read back from) with a real
LlamaIndex VectorStoreIndex: local Ollama embeddings (nomic-embed-text) backed
by the Qdrant instance Adiyan already runs against.
"""
import logging
from typing import Dict, List, Optional

from llama_index.core import Document, VectorStoreIndex
from llama_index.core.vector_stores import FilterOperator, MetadataFilter, MetadataFilters
from llama_index.embeddings.ollama import OllamaEmbedding
from llama_index.vector_stores.qdrant import QdrantVectorStore
from qdrant_client import QdrantClient

logger = logging.getLogger('MemoryIndex')

# New name, not the old 'coaching_history' the placeholder-vector code used to write to
# (that collection held only fake constant vectors - nothing worth migrating from it).
COLLECTION_NAME = 'adiyan_coaching_memory'
DEFAULT_TOP_K = 3

_instances: Dict[tuple, 'MemoryIndex'] = {}


class MemoryIndex:
    """One LlamaIndex VectorStoreIndex over Qdrant, shared by StorageAgent (writes)
    and LLMAgent (reads) so both hit the same collection with the same embedding model."""

    def __init__(self, qdrant_url: str, ollama_url: str, embed_model_name: str = 'nomic-embed-text'):
        self.embed_model = OllamaEmbedding(model_name=embed_model_name, base_url=ollama_url)
        client = QdrantClient(url=qdrant_url)
        vector_store = QdrantVectorStore(client=client, collection_name=COLLECTION_NAME)
        self.index = VectorStoreIndex.from_vector_store(vector_store, embed_model=self.embed_model)

    def insert(self, text: str, contact_name: str, persona: Optional[str], timestamp: str, message_id: str):
        """Embed and store one interaction for later semantic recall."""
        doc = Document(
            text=text,
            metadata={
                'contact_name': contact_name,
                'persona': persona or '',
                'timestamp': timestamp,
                'message_id': message_id,
            },
        )
        self.index.insert(doc)

    def retrieve(self, query: str, contact_name: str, top_k: int = DEFAULT_TOP_K) -> List[str]:
        """Return up to top_k past interaction texts for this contact, most relevant first."""
        filters = MetadataFilters(filters=[
            MetadataFilter(key='contact_name', value=contact_name, operator=FilterOperator.EQ)
        ])
        retriever = self.index.as_retriever(similarity_top_k=top_k, filters=filters)
        nodes = retriever.retrieve(query)
        return [n.node.get_content() for n in nodes]


def get_memory_index(qdrant_url: str, ollama_url: str) -> Optional[MemoryIndex]:
    """Cached factory - returns None (not an error) if Qdrant/Ollama aren't reachable,
    so callers can treat memory as an optional enhancement rather than a hard dependency."""
    key = (qdrant_url, ollama_url)
    if key not in _instances:
        try:
            _instances[key] = MemoryIndex(qdrant_url, ollama_url)
        except Exception as e:
            logger.warning(f"⚠️  Memory index unavailable ({e}) - continuing without it")
            _instances[key] = None
    return _instances[key]
