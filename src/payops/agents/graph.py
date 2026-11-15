"""Builds the LangGraph workflow.

    triage -> research -> drafting -> policy -> finalize -> (next invoice) research ...
                              ^          |
                              +- revise -+   (one retry after a discount promise)

No checkpointer and no interrupts: a run that needs approval simply ends as
``awaiting_approval``, and approving executes the stored draft without the graph.
"""

from functools import partial
from typing import Any

from langgraph.graph import END, START, StateGraph

from payops.agents.nodes.drafting import drafting
from payops.agents.nodes.finalize import finalize, route_next_invoice
from payops.agents.nodes.policy_node import policy, route_after_policy
from payops.agents.nodes.research import research
from payops.agents.nodes.triage import triage
from payops.agents.state import AgentContext, AgentState

# Nodes per invoice in the worst case: research, drafting, policy, drafting, policy, finalize.
STEPS_PER_INVOICE = 6


def build_graph(ctx: AgentContext) -> Any:
    graph = StateGraph(AgentState)
    graph.add_node("triage", partial(triage, ctx=ctx))
    graph.add_node("research", partial(research, ctx=ctx))
    graph.add_node("drafting", partial(drafting, ctx=ctx))
    graph.add_node("policy", partial(policy, ctx=ctx))
    graph.add_node("finalize", partial(finalize, ctx=ctx))

    graph.add_edge(START, "triage")
    graph.add_conditional_edges("triage", route_next_invoice, {"research": "research", "end": END})
    graph.add_edge("research", "drafting")
    graph.add_edge("drafting", "policy")
    graph.add_conditional_edges(
        "policy", route_after_policy, {"drafting": "drafting", "finalize": "finalize"}
    )
    graph.add_conditional_edges(
        "finalize", route_next_invoice, {"research": "research", "end": END}
    )
    return graph.compile()


def recursion_limit(invoice_count: int) -> int:
    return STEPS_PER_INVOICE * max(invoice_count, 1) + 10
