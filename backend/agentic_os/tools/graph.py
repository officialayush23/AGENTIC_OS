"""Knowledge-graph tools and the plain `respond_to_user` step."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from .base import Check, RiskLevel, ToolContext, ToolError, ToolSpec


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(extra="allow")


class BuildInput(_In):
    deep: bool = Field(False, description="Also let the AI provider read documents and extract typed relations (slower).")
    path_prefix: Optional[str] = Field(None, max_length=500, description="Only documents under this folder.")


def _build(ctx: ToolContext, args: BuildInput) -> dict:
    builder = (ctx.services or {}).get("kg_build")
    if builder is None:
        raise ToolError("KG_UNAVAILABLE", "The knowledge graph is not available.")
    return builder(deep=args.deep, path_prefix=args.path_prefix, cancel=ctx.cancel_check)


class QueryInput(_In):
    query: str = Field(min_length=1, max_length=200, description="An entity name, e.g. a person, project or concept.")
    limit: int = Field(8, ge=1, le=30)


def _query(ctx: ToolContext, args: QueryInput) -> dict:
    kg = (ctx.services or {}).get("kg")
    if kg is None:
        raise ToolError("KG_UNAVAILABLE", "The knowledge graph is not available.")
    matches = kg.search(args.query, args.limit)
    detail = kg.node(matches[0]["id"]) if matches else None
    if detail:
        detail = {**detail, "out": detail["out"][:25], "in": detail["in"][:25]}
    return {"query": args.query, "matches": matches, "best": detail}


class RespondInput(_In):
    text: str = Field(min_length=1, max_length=20000, description="The answer to show (and speak) to the user.")


def _respond(ctx: ToolContext, args: RespondInput) -> dict:
    return {"text": args.text}


GRAPH_TOOLS = [
    ToolSpec(name="build_knowledge_graph", version="1.0",
             description="Build (or rebuild) the knowledge graph of people, projects, concepts, topics, tags, modules "
                         "and documents from everything indexed, plus email and calendar. deep=true also uses the AI provider.",
             input_model=BuildInput, output_model=_Out, capabilities=("workspace.read",), risk=RiskLevel.READ_ONLY,
             timeout_s=600, mutates=False, idempotent=True, retryable=False, approval="never",
             verification="Counts are returned.", handler=_build,
             verify=lambda c, a, o: [Check("graph_built", o.get("nodes", 0) >= 0, f"{o.get('nodes')} nodes")],
             category="knowledge", examples=({"deep": False},)),
    ToolSpec(name="kg_query", version="1.0",
             description="Look up an entity in the knowledge graph: what it is connected to and which documents mention it.",
             input_model=QueryInput, output_model=_Out, capabilities=("workspace.read",), risk=RiskLevel.READ_ONLY,
             timeout_s=20, mutates=False, idempotent=True, retryable=True, approval="never", verification="None.",
             handler=_query, category="knowledge", examples=({"query": "kernel"},)),
    ToolSpec(name="respond_to_user", version="1.0",
             description="Answer the user directly in the conversation (for questions, explanations and small talk that "
                         "need no other tool). Use it as the last step to give the final answer.",
             input_model=RespondInput, output_model=_Out, capabilities=("compute",), risk=RiskLevel.READ_ONLY,
             timeout_s=5, mutates=False, idempotent=True, retryable=True, approval="never", verification="None.",
             handler=_respond, category="conversation", examples=({"text": "Hello! What should we work on?"},)),
]
