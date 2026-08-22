"""Tool registry and — more importantly — the exposure rules.

This module is the architectural claim of the whole demo. The system prompt
does not *ask* the agent to verify before reading an order. Before
verification, ``get_order_status`` is simply not in the tool list the model
receives. You cannot prompt-inject your way to a tool that was never sent.

The model chooses the words. This table chooses what is possible.
"""
import logging
import time

from app.identity import AuthorizationError
from app.tools import handoff, kb, orders, returns, verification
from app.tools.base import Tool, ToolContext

log = logging.getLogger("bookly.tools")

ALL: dict[str, Tool] = {
    t.name: t
    for t in kb.TOOLS + verification.TOOLS + orders.TOOLS + returns.TOOLS + handoff.TOOLS
}

# Available in every state, to anyone. Neither reads customer data.
ALWAYS = ["search_help_center", "escalate_to_human"]

# Which intents need to know who the customer is before they can be served.
# "policy_question" is absent on purpose: making someone log in to read the
# published returns policy is friction that buys nothing.
INTENT_REQUIRES_IDENTITY = {"order_status", "return_refund"}

# Unlocked only once identity is established.
INTENT_TOOLS = {
    "order_status": ["list_my_orders", "get_order_status"],
    "return_refund": [
        "list_my_orders",
        "get_order_status",
        "check_return_eligibility",
        "create_return",
    ],
    "policy_question": [],
    "unclear": [],
}


def tools_for(intent: str, verified: bool, locked: bool) -> list[dict]:
    """The tool list for this turn, given where the conversation stands."""
    names = list(ALWAYS)
    needs_id = intent in INTENT_REQUIRES_IDENTITY

    if needs_id and not verified and not locked:
        names.append("verify_customer")
    if verified:
        names.extend(INTENT_TOOLS.get(intent, []))

    # Stable order keeps the tools block byte-identical across turns, which is
    # what lets the prompt cache hit.
    ordered = sorted(set(names))
    return [ALL[n].schema() for n in ordered]


def withheld(exposed: list[str]) -> list[str]:
    """Tools that exist but were NOT sent to the model this turn. This is the
    security story made visible: absence, not discouragement."""
    return sorted(set(ALL) - set(exposed))


def dispatch(name: str, args: dict, ctx: ToolContext) -> tuple[dict, bool, float]:
    """Execute one tool call. Returns (result, is_error, elapsed_ms)."""
    started = time.perf_counter()
    tool = ALL.get(name)
    if tool is None:
        # Reachable if the model hallucinates a tool name.
        return {"error": f"No tool named {name}."}, True, 0.0

    try:
        result = tool.handler(ctx, **args)
        is_error = False
    except AuthorizationError as exc:
        # Belt and braces: the tool should not have been exposed at all. If we
        # land here the exposure table and the auth state disagree, which is a
        # bug worth seeing in the trace rather than papering over.
        log.warning("authorization refused for %s: %s", name, exc)
        result = {
            "error": "not_authorized",
            "detail": str(exc),
            "note": "Ask the customer to verify their identity first.",
        }
        is_error = True
    except Exception as exc:  # noqa: BLE001 - surfaced to the model as a tool error
        log.exception("tool %s failed", name)
        result = {"error": "tool_failed", "detail": str(exc)}
        is_error = True

    return result, is_error, (time.perf_counter() - started) * 1000
