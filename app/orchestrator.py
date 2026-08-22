"""The agent loop.

Four steps, run every turn:

  1. NEED  - classify intent with a separate constrained call
  2. KNOW  - decide whether this intent requires identity, and gate tools on it
  3. SERVE - run the tool loop with only the tools this state permits
  4. RESOLVE - finish, or hand to a human rather than improvise

Every step is a mix. The model does the probabilistic, language-shaped part --
reading a messy sentence to propose an intent in step 1, collecting the two
identity factors in step 2, choosing a tool and writing the reply in step 3.
Code owns every decision that follows from it: the confidence floor and routing
table here, the credential comparison in ``identity.py``, the tool-exposure
table in ``tools/__init__.py``, the return policy in ``tools/returns.py``.

The model proposes. Code disposes. Nothing probabilistic is ever the last word.
"""
import json
import logging
import time
from datetime import date

from app import config, db, prompts, tools as tool_registry
from app.llm import get_engine
from app.llm import heuristics
from app.state import Session, redact
from app.tools.base import ToolContext

log = logging.getLogger("bookly.orchestrator")

_engine = None


def engine():
    global _engine
    if _engine is None:
        _engine = get_engine()
    return _engine


def set_engine(new_engine) -> None:
    """Swap the model at runtime. Used by the /api/engine endpoint so a key can
    be supplied from the UI without restarting anything."""
    global _engine
    _engine = new_engine
    log.info("engine switched to %s", new_engine.name)


def _classifier_transcript(session: Session, keep: int = 6) -> list[dict]:
    """Plain text only. The router does not need tool payloads, and feeding it
    tool output invites it to route on what we found rather than what was asked."""
    out = []
    for msg in session.messages:
        if msg["role"] == "user" and isinstance(msg["content"], str):
            out.append({"role": "user", "content": msg["content"]})
        elif msg["role"] == "assistant" and isinstance(msg["content"], list):
            text = " ".join(b["text"] for b in msg["content"] if b.get("type") == "text").strip()
            if text and out:
                out.append({"role": "assistant", "content": text})
    # The API requires the first message to be from the user.
    trimmed = out[-keep:]
    while trimmed and trimmed[0]["role"] != "user":
        trimmed.pop(0)
    return trimmed or out[:1]


def _route(session: Session, classification: dict) -> str:
    """Low confidence does not mean 'unclear' — it means 'do not change your
    mind'. A customer replying '94110' to a verification prompt is
    unclassifiable on its own but obviously continues the intent in play."""
    if classification["confidence"] >= prompts.CONFIDENCE_FLOOR:
        return classification["intent"]
    return session.intent if session.intent != "unclear" else "unclear"


def _token_summary(session: Session) -> dict | None:
    """The claims worth showing, never the token itself."""
    if not session.auth.verified or not session.auth.token:
        return None
    from app import identity

    try:
        claims = identity.authorize(session.auth.token, "orders:read")
    except identity.AuthorizationError:
        return {"state": "expired"}
    return {
        "state": "valid",
        "sub": claims["sub"],
        "scope": claims["scope"],
        "amr": claims.get("amr", []),
        "expires_in": max(0, claims["exp"] - int(time.time())),
        "jti": claims["jti"],
    }


def _force_escalation(session: Session, reason_code: str, summary: str) -> dict:
    """Used on paths where we will not let the model decide, e.g. a safety
    refusal or a runaway tool loop."""
    ctx = ToolContext(token=session.auth.token, session=session)
    result, _, _ = tool_registry.dispatch(
        "escalate_to_human", {"reason_code": reason_code, "summary": summary}, ctx
    )
    return result


def handle_turn(session: Session, user_text: str) -> dict:
    """Process one customer message. Returns the reply plus a full trace."""
    turn_started = time.perf_counter()
    queries = db.start_query_log()
    session.messages.append({"role": "user", "content": user_text})

    # --- 1. NEED ----------------------------------------------------------
    llm_error: str | None = None
    try:
        classification = engine().classify(_classifier_transcript(session))
    except Exception as exc:  # noqa: BLE001
        # A router outage must not take the conversation down with it -- but
        # falling back to "unclear" is not degrading gracefully, it is quietly
        # disarming the agent. `unclear` exposes no identity tools, so an agent
        # that lands there can never verify anyone and will escalate every
        # conversation while looking healthy.
        #
        # Fail over to the keyword router instead. It is worse than the model
        # and far better than nothing.
        log.warning("router call failed (%s), falling back to keywords: %s",
                    type(exc).__name__, exc)
        llm_error = type(exc).__name__
        classification = heuristics.classify_intent(_classifier_transcript(session))
        classification["reasoning"] = f"router unavailable ({llm_error}); routed on keywords"
    session.intent = _route(session, classification)

    # --- 2. KNOW ----------------------------------------------------------
    # Volatile state is computed per turn and handed to the engine, which
    # decides how to deliver it. It is deliberately NOT interpolated into the
    # cached system prompt (that would invalidate the cache every turn) and
    # deliberately not stored in `messages` (transports differ by model).
    state = prompts.state_block(session, str(date.today()))

    tool_calls_trace: list[dict] = []
    llm_ms = 0.0
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}
    guardrails: list[str] = []
    # A turn can produce several messages: "let me check that" before a tool
    # call, then the answer after it. The customer sees both, so both count as
    # the reply. Keeping only the last block silently swallowed half the turn.
    reply_parts: list[str] = []
    exposed: list[str] = []

    # --- 3. SERVE ---------------------------------------------------------
    for iteration in range(config.MAX_TOOL_ITERATIONS):
        # Recomputed every iteration: verifying inside this loop widens the
        # tool list for the very next model call, with no extra round trip.
        tool_schemas = tool_registry.tools_for(
            session.intent, session.auth.verified, session.auth.locked
        )
        exposed = [t["name"] for t in tool_schemas]

        try:
            result = engine().respond(session.messages, tool_schemas, state)
        except Exception as exc:  # noqa: BLE001
            # Bad key, rate limit, network. A customer should get a person, not
            # a stack trace, and the human should get everything gathered so far.
            log.error("model call failed: %s: %s", type(exc).__name__, exc)
            llm_error = type(exc).__name__
            guardrails.append(f"llm_error:{llm_error}")
            escalation = _force_escalation(
                session, "agent_uncertain",
                f"Model backend unavailable ({llm_error}); conversation needs a human.",
            )
            failure_text = (
                "I'm having trouble reaching my systems right now, so I've passed you "
                f"to a colleague — your reference is {escalation['ticket']}."
            )
            reply_parts.append(failure_text)
            session.messages.append(
                {"role": "assistant", "content": [{"type": "text", "text": failure_text}]}
            )
            break

        llm_ms += result.latency_ms
        for k in usage:
            usage[k] += result.usage.get(k, 0)

        if result.refused:
            # The model declined on safety grounds. That is a handoff, not an
            # error page shown to a customer.
            guardrails.append("model_refusal")
            escalation = _force_escalation(
                session, "out_of_scope",
                "Model declined to answer on safety grounds; needs human review.",
            )
            refusal_text = (
                "I'm not able to help with that one, so I've passed you to a "
                f"colleague — your reference is {escalation['ticket']}."
            )
            reply_parts.append(refusal_text)
            session.messages.append(
                {"role": "assistant", "content": [{"type": "text", "text": refusal_text}]}
            )
            break

        session.messages.append({"role": "assistant", "content": result.content})
        if result.text():
            reply_parts.append(result.text())

        calls = result.tool_calls()
        if not calls:
            break

        tool_result_blocks = []
        ctx = ToolContext(token=session.auth.token, session=session)
        for call in calls:
            payload, is_error, elapsed = tool_registry.dispatch(call["name"], call["input"], ctx)
            if is_error:
                guardrails.append(f"tool_error:{call['name']}")
            tool_calls_trace.append(
                {
                    "tool": call["name"],
                    "arguments": redact(call["input"]),
                    "ms": round(elapsed, 1),
                    "error": is_error,
                    "result": redact(payload),
                }
            )
            tool_result_blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call["id"],
                    # JSON text, not a nested object: that is what the API takes.
                    "content": json.dumps(payload, default=str),
                    "is_error": is_error,
                }
            )
        session.messages.append({"role": "user", "content": tool_result_blocks})
    else:
        # Loop budget exhausted. Rather than let it spin, hand off.
        guardrails.append("tool_loop_budget_exhausted")
        escalation = _force_escalation(
            session, "agent_uncertain",
            "Agent exceeded its tool-call budget without reaching an answer.",
        )
        exhausted_text = (
            "This is taking me longer than it should, so I've handed it to a colleague — "
            f"your reference is {escalation['ticket']}."
        )
        reply_parts.append(exhausted_text)
        session.messages.append(
            {"role": "assistant", "content": [{"type": "text", "text": exhausted_text}]}
        )

    reply = "\n\n".join(p for p in reply_parts if p).strip()
    if not reply:
        reply = "Sorry — I lost my thread there. Could you say that once more?"

    turn = {
        "turn": session.turn_count(),
        "engine": engine().name,
        "intent": session.intent,
        "intent_confidence": round(classification["confidence"], 2),
        "intent_reasoning": classification.get("reasoning"),
        "auth": session.auth.public(),
        "tools_exposed": exposed,
        "tools_withheld": tool_registry.withheld(exposed),
        "token": _token_summary(session),
        "queries": queries,
        "tool_calls": tool_calls_trace,
        "guardrails_triggered": guardrails,
        "llm_error": llm_error,
        "escalated": session.escalated,
        "llm_ms": round(llm_ms, 1),
        "total_ms": round((time.perf_counter() - turn_started) * 1000, 1),
        "usage": usage,
    }
    session.trace.append(turn)
    # One structured line per turn: this is what a CX ops team actually reads.
    log.info("turn %s", json.dumps(turn, default=str))
    return {"reply": reply, "trace": turn, "session_id": session.session_id}
