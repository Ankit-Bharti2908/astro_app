"""Response graph: load_session → analyze → learn → route → … → maybe_compact."""

from __future__ import annotations

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import RetryPolicy

from app.graphs.deps import Deps
from app.graphs.nodes.analyze import make_analyze, route_from_state
from app.graphs.nodes.answer_agent import make_answer_agent, route_after_answer
from app.graphs.nodes.common import llm_failure_handler
from app.graphs.nodes.compact import make_maybe_compact
from app.graphs.nodes.followup import make_followup
from app.graphs.nodes.learn import make_learn
from app.graphs.nodes.load_session import make_load_session
from app.graphs.nodes.memory_cmd import make_memory_cmd
from app.graphs.nodes.memory_recall import make_memory_recall
from app.graphs.nodes.smalltalk import make_smalltalk
from app.graphs.state import ChatState
from app.llm.provider import LLMUnavailable

LLM_RETRY = RetryPolicy(max_attempts=2, initial_interval=0.2, max_interval=1.0, retry_on=LLMUnavailable)


def build_response_graph(deps: Deps, checkpointer: BaseCheckpointSaver | None = None):
    g = StateGraph(ChatState)
    g.add_node("load_session", make_load_session(deps))
    g.add_node("analyze", make_analyze(deps))
    g.add_node("learn", make_learn(deps))
    g.add_node("answer_agent", make_answer_agent(deps), retry_policy=LLM_RETRY, error_handler=llm_failure_handler)
    g.add_node("fetch_context", ToolNode(deps.tools, name="fetch_context"))
    g.add_node("followup", make_followup(deps), retry_policy=LLM_RETRY, error_handler=llm_failure_handler)
    g.add_node("memory_recall", make_memory_recall(deps), retry_policy=LLM_RETRY, error_handler=llm_failure_handler)
    g.add_node("memory_cmd", make_memory_cmd(deps))
    g.add_node("smalltalk", make_smalltalk(deps), retry_policy=LLM_RETRY, error_handler=llm_failure_handler)
    g.add_node("maybe_compact", make_maybe_compact(deps))

    g.add_edge(START, "load_session")
    g.add_edge("load_session", "analyze")
    g.add_edge("analyze", "learn")
    g.add_conditional_edges(
        "learn",
        route_from_state,
        {
            "answer": "answer_agent",
            "followup": "followup",
            "memory_recall": "memory_recall",
            "memory_cmd": "memory_cmd",
            "smalltalk": "smalltalk",
        },
    )
    g.add_conditional_edges("answer_agent", route_after_answer, {"fetch_context": "fetch_context", "maybe_compact": "maybe_compact"})
    g.add_edge("fetch_context", "answer_agent")
    for node in ("followup", "memory_recall", "memory_cmd", "smalltalk"):
        g.add_edge(node, "maybe_compact")
    g.add_edge("maybe_compact", END)
    return g.compile(checkpointer=checkpointer)
