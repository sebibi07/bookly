"""Deterministic scripted engine.

Not a toy stand-in for the demo — a hedge. Conference wifi dies, keys get
rotated, rate limits happen. `BOOKLY_MOCK_LLM=1` runs the *entire* architecture
(routing, tool exposure, JWT scoping, the policy engine, escalation) with the
model swapped for rules, so the thing being demonstrated still works. It is
also what lets the eval suite assert on orchestration behaviour without the
noise of model sampling.

It reads the same transcript and the same tool list the real model gets, and
can only call tools that were actually exposed.
"""
import itertools
import json

from app.llm import LLMResult
from app.llm.heuristics import (
    DAMAGE_WORDS, EMAIL_RE, HUMAN_WORDS, INJECTION_MARKERS, ORDER_RE, POLICY_WORDS,
    REASON_HINTS, RETURN_WORDS, STATUS_WORDS, ZIP_RE, classify_intent, user_texts,
)

_ids = itertools.count(1)

def _text(text: str) -> LLMResult:
    return LLMResult(content=[{"type": "text", "text": text}], stop_reason="end_turn")


def _call(name: str, args: dict, preamble: str | None = None) -> LLMResult:
    content = []
    if preamble:
        content.append({"type": "text", "text": preamble})
    content.append(
        {"type": "tool_use", "id": f"toolu_mock_{next(_ids)}", "name": name, "input": args}
    )
    return LLMResult(content=content, stop_reason="tool_use")


def _pending_results(messages) -> dict:
    """Tool results from the turn immediately preceding, keyed by tool name."""
    for msg in reversed(messages):
        if msg["role"] == "system":
            continue
        if msg["role"] == "user" and isinstance(msg["content"], list):
            names = {}
            # Match each result back to the tool_use that produced it.
            uses = {}
            for prior in messages:
                if prior["role"] == "assistant" and isinstance(prior["content"], list):
                    for b in prior["content"]:
                        if b.get("type") == "tool_use":
                            uses[b["id"]] = b["name"]
            for block in msg["content"]:
                if block.get("type") == "tool_result":
                    # Tool results travel as JSON text, which is what the real
                    # API accepts; parse them back to inspect them.
                    try:
                        payload = json.loads(block["content"])
                    except (TypeError, ValueError):
                        payload = {}
                    names[uses.get(block["tool_use_id"], "?")] = payload
            return names
        return {}
    return {}


def _slots(messages) -> dict:
    """Newest mention wins. A customer who says "actually, check BK-10102"
    means that one, not the first order number they typed ten turns ago."""
    texts = user_texts(messages)

    def latest(pattern, group=0):
        for text in reversed(texts):
            match = pattern.search(text)
            if match:
                return match.group(group)
        return None

    reason = None
    for text in reversed(texts):
        low = text.lower()
        for hints, value in REASON_HINTS:
            if any(h in low for h in hints):
                reason = value
                break
        if reason:
            break

    order = latest(ORDER_RE, 1)
    return {
        "email": latest(EMAIL_RE),
        "zip": latest(ZIP_RE, 1),
        "order": f"BK-{order}" if order else None,
        "reason": reason,
    }


class MockEngine:
    name = "mock"

    def classify(self, transcript: list[dict]) -> dict:
        return classify_intent(transcript)

    def respond(self, messages: list[dict], tools: list[dict],
                state: str | None = None) -> LLMResult:
        available = {t["name"] for t in tools}
        results = _pending_results(messages)
        slots = _slots(messages)
        texts = user_texts(messages)
        last = (texts[-1] if texts else "").lower()

        if results:
            return self._after_tools(results, slots, available)
        return self._fresh(last, slots, available)

    # -- responding to tool output -----------------------------------------
    def _after_tools(self, results, slots, available) -> LLMResult:
        if "escalate_to_human" in results:
            r = results["escalate_to_human"]
            emailed = (
                f" I've emailed a copy to {r.get('emailed_to')}, so you have it in writing."
                if r.get("email_sent") else ""
            )
            return _text(
                f"I've passed this to a colleague — your reference is {r.get('ticket')}."
                f"{emailed} They have the whole conversation, so you won't need to repeat "
                f"yourself. Typical wait is {r.get('queue_wait_estimate')}."
            )

        if "verify_customer" in results:
            r = results["verify_customer"]
            if r.get("locked"):
                return _call(
                    "escalate_to_human",
                    {"reason_code": "identity_verification_failed",
                     "summary": "Customer could not verify after three attempts. Needs manual identity check."},
                    "I'm not able to verify the account from here, so let me get a colleague involved.",
                )
            if not r.get("verified"):
                return _text(
                    "Those details don't match an account I can see. Could you double-check "
                    f"both the email address and the shipping ZIP? ({r.get('attempts_remaining')} attempts left.)"
                )
            first = (r.get("customer_name") or "there").split()[0]
            if slots["reason"] and slots["order"] and "check_return_eligibility" in available:
                return _call(
                    "check_return_eligibility",
                    {"order_number": slots["order"], "reason": slots["reason"]},
                    f"Thanks {first}, you're verified — let me check that order.",
                )
            if slots["order"] and "get_order_status" in available:
                return _call("get_order_status", {"order_number": slots["order"]},
                             f"Thanks {first}, you're verified. Let me pull that up.")
            if "list_my_orders" in available:
                return _call("list_my_orders", {}, f"Thanks {first}, you're verified — one moment.")
            return _text(f"Thanks {first}, you're verified. What can I help with?")

        if "list_my_orders" in results:
            r = results["list_my_orders"]
            orders = r.get("orders", [])
            if slots["order"] and "get_order_status" in available:
                return _call("get_order_status", {"order_number": slots["order"]})
            if len(orders) > 1:
                listed = "; ".join(
                    f"{o['order_number']} ({o['status'].replace('_', ' ')}, placed {o['placed_on']})"
                    for o in orders[:4]
                )
                return _text(
                    f"You've got {len(orders)} orders with us — {listed}. Which one did you mean?"
                )
            if orders:
                return _call("get_order_status", {"order_number": orders[0]["order_number"]})
            return _text("I don't see any orders on this account yet.")

        if "get_order_status" in results:
            r = results["get_order_status"]
            if not r.get("found"):
                return _text(
                    "I can't see that order number on your account. Want me to list the "
                    "orders I can see?"
                )
            if r.get("requires_human_review"):
                return _call(
                    "escalate_to_human",
                    {"reason_code": "carrier_trace_required",
                     "summary": f"Order {r['order_number']} is {r['days_past_estimated_delivery']} days "
                                f"past ETA with no delivery scan ({r['carrier']} {r['tracking_number']}). "
                                "Needs a carrier trace."},
                    f"Order {r['order_number']} is {r['days_past_estimated_delivery']} days past its "
                    "estimated delivery and the carrier hasn't scanned it recently. I don't want to "
                    "guess at a new date — let me open a trace with a colleague.",
                )
            if r.get("status") == "delivered":
                return _text(
                    f"{r['order_number']} was delivered on {r['delivered_on']} by {r['carrier']} "
                    f"({r['tracking_number']}). Anything not right with it?"
                )
            return _text(
                f"{r['order_number']} is {r['status_in_plain_english']} — {r['carrier']} has it, "
                f"tracking {r['tracking_number']}, estimated to reach you on {r['estimated_delivery']}."
            )

        if "check_return_eligibility" in results:
            r = results["check_return_eligibility"]
            if not r.get("found"):
                return _text("I can't see that order on your account.")
            if r.get("eligible"):
                return _text(
                    f"Good news — {r['order_number']} is inside the {r['window_days']}-day window, "
                    f"so that's a {r['refund_amount']} refund and return shipping is "
                    f"{r['return_shipping']}. "
                    "Want me to set the return up now?"
                )
            if r.get("code") == "window_expired":
                return _call(
                    "escalate_to_human",
                    {"reason_code": "policy_exception_requested",
                     "summary": f"{r['order_number']} delivered {r['days_since_delivery']} days ago, "
                                f"outside the {r['window_days']}-day window. Customer would like it reviewed."},
                    f"I'm sorry — that one was delivered {r['days_since_delivery']} days ago and our "
                    f"window is {r['window_days']} days, so I can't approve it myself. Let me put it "
                    "in front of someone who can look at an exception.",
                )
            return _text(r.get("explanation", "That return can't be started right now."))

        if "create_return" in results:
            r = results["create_return"]
            if not r.get("created"):
                return _text(r.get("explanation", "I wasn't able to open that return."))
            emailed = (
                f" I've emailed the prepaid label to {r.get('emailed_to')}."
                if r.get("email_sent") else ""
            )
            return _text(
                f"Done — your RMA is {r['rma']}.{emailed} Refund of {r['refund_amount']} "
                "lands 5-7 business days after the book reaches us."
            )

        if "search_help_center" in results:
            r = results["search_help_center"]
            if not r.get("found"):
                return _text(
                    "I couldn't find that in our help centre, and I'd rather not guess. "
                    "Want me to put you through to a colleague?"
                )
            article = r["articles"][0]
            return _text(f"{article['content']}\n\n(From our help centre: “{article['title']}”.)")

        return _text("Let me know how else I can help.")

    # -- responding to a fresh customer message -----------------------------
    def _fresh(self, last, slots, available) -> LLMResult:
        if any(m in last for m in INJECTION_MARKERS) or "another customer" in last:
            return _text(
                "I can only help with the account I've verified in this chat, and I can't "
                "share anyone else's details or talk about how I work. Happy to keep going "
                "with your own order though."
            )

        if any(w in last for w in HUMAN_WORDS):
            return _call(
                "escalate_to_human",
                {"reason_code": "customer_requested_human",
                 "summary": "Customer explicitly asked for a human agent."},
                "Of course — let me hand you over.",
            )

        if "search_help_center" in available and any(w in last for w in POLICY_WORDS) \
                and not slots["order"]:
            return _call("search_help_center", {"query": last})

        if "verify_customer" in available:
            if slots["email"] and slots["zip"]:
                return _call(
                    "verify_customer",
                    {"email": slots["email"], "shipping_zip": slots["zip"]},
                    "One moment while I check those.",
                )
            missing = "the ZIP code that order shipped to" if slots["email"] else \
                      ("the email address on your account" if slots["zip"] else
                       "the email address on your account and the ZIP code it shipped to")
            return _text(
                f"Happy to help with that. Before I can look at order details I need to check "
                f"it's your account — could you give me {missing}?"
            )

        # Confirmation must be checked before eligibility, or "yes, go ahead"
        # loops back into another eligibility check instead of writing.
        confirmed = any(w in last for w in ("yes", "go ahead", "please do", "set it up", "do it"))
        if "create_return" in available and confirmed and slots["order"] and slots["reason"]:
            return _call("create_return", {"order_number": slots["order"], "reason": slots["reason"]})

        if "check_return_eligibility" in available and slots["reason"]:
            if slots["order"]:
                return _call("check_return_eligibility",
                             {"order_number": slots["order"], "reason": slots["reason"]})
            return _call("list_my_orders", {})

        if "get_order_status" in available and slots["order"]:
            return _call("get_order_status", {"order_number": slots["order"]})

        if "list_my_orders" in available and any(w in last for w in STATUS_WORDS + RETURN_WORDS + DAMAGE_WORDS):
            return _call("list_my_orders", {})

        if any(w in last for w in RETURN_WORDS + DAMAGE_WORDS) and "check_return_eligibility" in available:
            return _text(
                "I can help with that. What went wrong — did it arrive damaged, was it the "
                "wrong book, or have you just changed your mind?"
            )

        return _text(
            "Happy to help. Is this about where an order has got to, returning something, "
            "or a general question about how Bookly works?"
        )
