"""Memory graph (§5): extract → score → filter → Send(reconcile)* → write."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from app.graphs.deps import Deps
from app.graphs.nodes.memory_nodes import (
    dispatch_reconcile,
    extract_failure_handler,
    make_extract,
    make_filter,
    make_reconcile,
    make_score,
    make_write,
)
from app.graphs.state import MemoryState
from app.llm.provider import LLMError

LLM_RETRY = RetryPolicy(max_attempts=2, initial_interval=0.2, max_interval=1.0, retry_on=LLMError)


def build_memory_graph(deps: Deps):
    g = StateGraph(MemoryState)
    g.add_node("extract", make_extract(deps), retry_policy=LLM_RETRY, error_handler=extract_failure_handler)
    g.add_node("score", make_score(deps))
    g.add_node("filter", make_filter(deps))
    g.add_node("reconcile", make_reconcile(deps))
    g.add_node("write", make_write(deps))

    g.add_edge(START, "extract")
    g.add_edge("extract", "score")
    g.add_edge("score", "filter")
    g.add_conditional_edges("filter", dispatch_reconcile, ["reconcile", END])
    g.add_edge("reconcile", "write")
    g.add_edge("write", END)
    return g.compile()
