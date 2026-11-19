"""Local RAG: embeddings + SQLite vector/FTS store + hybrid retrieval. See docs/RAG.md."""

from .chunker import Chunk, chunk_text
from .embedders import DEFAULTS as EMBEDDER_DEFAULTS
from .embedders import Embedder, EmbedderError, LocalHashEmbedder, build_embedder
from .knowledge import ANSWER_SYSTEM, KnowledgeBase, build_answer_prompt

__all__ = ["ANSWER_SYSTEM", "Chunk", "EMBEDDER_DEFAULTS", "Embedder", "EmbedderError", "KnowledgeBase",
           "LocalHashEmbedder", "build_answer_prompt", "build_embedder", "chunk_text"]
