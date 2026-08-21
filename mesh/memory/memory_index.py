"""Real semantic memory over past coaching interactions, plus the coach's uploaded
knowledge base.

Replaces the old placeholder-vector write StorageAgent used to do (a constant
[0.1]*384 vector, into a collection nothing ever read back from) with a real
LlamaIndex VectorStoreIndex: local Ollama embeddings (nomic-embed-text) backed
by the Qdrant instance Adiyan already runs against.

Two separate collections, same embedding model and Qdrant instance:
- adiyan_coaching_memory: per-contact conversation history (existing).
- adiyan_knowledge_base: documents the coach uploads (PDFs, via Docling) -
  global, not scoped to any one contact.
"""
import io
import logging
from typing import Dict, List, Optional

from llama_index.core import Document, VectorStoreIndex
from llama_index.core.node_parser import SentenceSplitter
from llama_index.core.vector_stores import FilterOperator, MetadataFilter, MetadataFilters
from llama_index.embeddings.ollama import OllamaEmbedding
from llama_index.vector_stores.qdrant import QdrantVectorStore
from qdrant_client import QdrantClient

logger = logging.getLogger('MemoryIndex')

# New name, not the old 'coaching_history' the placeholder-vector code used to write to
# (that collection held only fake constant vectors - nothing worth migrating from it).
COLLECTION_NAME = 'adiyan_coaching_memory'
KB_COLLECTION_NAME = 'adiyan_knowledge_base'
DEFAULT_TOP_K = 3
KB_DEFAULT_TOP_K = 4
KB_CHUNK_SIZE = 800
KB_CHUNK_OVERLAP = 100

_instances: Dict[tuple, 'MemoryIndex'] = {}


class MemoryIndex:
    """Two LlamaIndex VectorStoreIndexes over the same Qdrant instance and embedding
    model: conversation memory (StorageAgent writes, LLMAgent reads) and the
    knowledge base (kb_ingestion_poller writes, LLMAgent reads)."""

    def __init__(self, qdrant_url: str, ollama_url: str, embed_model_name: str = 'nomic-embed-text'):
        self.embed_model = OllamaEmbedding(model_name=embed_model_name, base_url=ollama_url)
        client = QdrantClient(url=qdrant_url)

        vector_store = QdrantVectorStore(client=client, collection_name=COLLECTION_NAME)
        self.index = VectorStoreIndex.from_vector_store(vector_store, embed_model=self.embed_model)

        kb_vector_store = QdrantVectorStore(client=client, collection_name=KB_COLLECTION_NAME)
        self.kb_index = VectorStoreIndex.from_vector_store(kb_vector_store, embed_model=self.embed_model)
        self._splitter = SentenceSplitter(chunk_size=KB_CHUNK_SIZE, chunk_overlap=KB_CHUNK_OVERLAP)

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

    def ingest_pdf(self, content: bytes, filename: str, timestamp: str) -> int:
        """Parse a PDF with Docling, chunk it, and embed each chunk into the knowledge
        base. Returns the number of chunks stored. Raises on parse failure - the caller
        (kb_ingestion_poller) is expected to report that back to the coach over WhatsApp,
        not swallow it, since this is a coach-initiated action they need feedback on."""
        from docling.document_converter import DocumentConverter
        from docling_core.types.io import DocumentStream

        converter = DocumentConverter()
        result = converter.convert(DocumentStream(name=filename, stream=io.BytesIO(content)))
        markdown = result.document.export_to_markdown()

        if not markdown.strip():
            raise ValueError(f"Docling extracted no text from '{filename}' (scanned image with no OCR text?)")

        chunks = self._splitter.split_text(markdown)
        for i, chunk in enumerate(chunks):
            self.kb_index.insert(Document(
                text=chunk,
                metadata={
                    'source_filename': filename,
                    'chunk_index': i,
                    'ingested_at': timestamp,
                },
            ))
        return len(chunks)

    def retrieve_knowledge_base(self, query: str, top_k: int = KB_DEFAULT_TOP_K) -> List[str]:
        """Return up to top_k relevant knowledge-base chunks, most relevant first.
        Global - not scoped to any one contact, unlike retrieve()."""
        retriever = self.kb_index.as_retriever(similarity_top_k=top_k)
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
