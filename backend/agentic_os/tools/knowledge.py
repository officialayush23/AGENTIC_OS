"""Knowledge-base tool: lets plans retrieve from the local RAG index.

Results are filtered through the caller's workspace view, so a process only
sees indexed passages from files its capabilities allow it to read, and never
from sensitive files.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

from .base import Check, RiskLevel, ToolContext, ToolError, ToolSpec


class KnowledgeSearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=500, description="What to look for, in natural language.")
    k: int = Field(6, ge=1, le=20, description="How many passages to return.")
    path_prefix: Optional[str] = Field(None, max_length=1024, description="Only search under this folder.")


class KnowledgeHit(BaseModel):
    source: str
    path: str
    start_line: int
    end_line: int
    score: float
    text: str


class KnowledgeSearchOutput(BaseModel):
    query: str
    embedder: str
    count: int
    hits: List[KnowledgeHit]


def _handler(ctx: ToolContext, args: KnowledgeSearchInput) -> dict:
    kb = (ctx.services or {}).get("rag")
    if kb is None:
        raise ToolError("KB_UNAVAILABLE", "The knowledge base is not available.")

    def allow(hit: dict) -> bool:
        if hit["source"] != "workspace":
            return True
        try:
            target = ctx.workspace.resolve(hit["path"])
        except ToolError:
            return False
        return not target.sensitive

    hits = kb.search(args.query, args.k, path_prefix=args.path_prefix, allow=allow)
    return {
        "query": args.query, "embedder": kb.embedder.name, "count": len(hits),
        "hits": [{"source": h["source"], "path": h["path"], "start_line": h["start_line"], "end_line": h["end_line"],
                  "score": h["score"], "text": h["text"][:1500]} for h in hits],
    }


def _verify(ctx: ToolContext, args: KnowledgeSearchInput, out: dict) -> List[Check]:
    return [Check("hit_count_matches", out.get("count") == len(out.get("hits") or []), f"{out.get('count')} hit(s)")]


KNOWLEDGE_TOOLS = [
    ToolSpec(
        name="search_knowledge", version="1.0",
        description="Search the user's local knowledge base (indexed workspace files and notes) by meaning and "
                    "keywords. Returns the best matching passages with file paths and line numbers.",
        input_model=KnowledgeSearchInput, output_model=KnowledgeSearchOutput, capabilities=("workspace.read",),
        risk=RiskLevel.READ_ONLY, timeout_s=30, mutates=False, idempotent=True, retryable=True, approval="never",
        verification="Hit count matches the returned passages.", handler=_handler, verify=_verify,
        audit_fields=("query",), category="knowledge",
        examples=({"query": "how is authentication configured", "k": 5},),
    ),
]
