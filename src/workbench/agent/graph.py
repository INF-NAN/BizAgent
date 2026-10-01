"""LangGraph wiring: intake -> plan -> act <-> observe -> verify -> respond; a call that needs approval
goes act -> preview -> approve (interrupt) -> observe, or back to plan when rejected."""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from workbench.agent.deps import AgentDeps
from workbench.agent.nodes.act import make_act
from workbench.agent.nodes.approve import make_approve
from workbench.agent.nodes.intake import make_intake
from workbench.agent.nodes.observe import make_observe
from workbench.agent.nodes.plan import make_plan
from workbench.agent.nodes.preview import make_preview
from workbench.agent.nodes.respond import make_respond
from workbench.agent.nodes.verify import make_verify
from workbench.agent.state import AgentState

ROUTES: dict[str, list[str]] = {
    "intake": ["plan"],
    "plan": ["act", "respond"],
    "act": ["preview", "observe", "verify", "respond"],
    "preview": ["approve"],
    "approve": ["observe", "plan"],
    "observe": ["act", "respond"],
    "verify": ["act", "respond"],
}


def _route(state: AgentState) -> str:
    return state.get("next", "respond")


def build_graph(deps: AgentDeps, checkpointer: Any = None) -> Any:
    g: Any = StateGraph(AgentState)  # langgraph overloads reject generic node factories
    g.add_node("intake", make_intake(deps))
    g.add_node("plan", make_plan(deps))
    g.add_node("act", make_act(deps))
    g.add_node("preview", make_preview(deps))
    g.add_node("approve", make_approve(deps))
    g.add_node("observe", make_observe(deps))
    g.add_node("verify", make_verify(deps))
    g.add_node("respond", make_respond(deps))
    g.add_edge(START, "intake")
    for node, targets in ROUTES.items():
        g.add_conditional_edges(node, _route, {t: t for t in targets})
    g.add_edge("respond", END)
    return g.compile(checkpointer=checkpointer)
