"""Return eligibility and creation.

The eligibility rules live in ``evaluate()`` -- ordinary Python, unit-testable,
identical on every call. They are not in the system prompt. A customer cannot
argue a Python function into extending a return window, and the same question
asked twice cannot get two answers.
"""
import logging
import secrets
from datetime import date

from app import config, db, notifications
from app.identity import AuthorizationError, customer_id_from
from app.tools.base import Tool, ToolContext, money, obj

log = logging.getLogger("bookly.tools.returns")

REASONS = ["damaged", "defective", "wrong_item", "changed_mind", "not_as_described"]
# Damage is Bookly's fault, so it buys a longer window. Encoding the exception
# here rather than in prose is what makes it auditable.
EXTENDED_WINDOW_REASONS = {"damaged", "defective"}


def evaluate(order: dict, reason: str) -> dict:
    """Pure policy. Given an order row and a reason, is a return allowed?"""
    window = (
        config.DAMAGED_RETURN_WINDOW_DAYS
        if reason in EXTENDED_WINDOW_REASONS
        else config.RETURN_WINDOW_DAYS
    )

    if order["existing_returns"]:
        rma = order["existing_returns"][0]["rma"]
        return {
            "eligible": False,
            "code": "already_returned",
            "explanation": f"A return ({rma}) is already open on this order.",
        }

    if order["status"] != "delivered" or not order["delivered_at"]:
        return {
            "eligible": False,
            "code": "not_yet_delivered",
            "explanation": (
                "This order has not been delivered yet, so a return cannot be "
                "started. If it arrives damaged, come back and we will handle it."
            ),
        }

    days_since = (date.today() - order["delivered_at"]).days
    if days_since > window:
        return {
            "eligible": False,
            "code": "window_expired",
            "window_days": window,
            "days_since_delivery": days_since,
            "explanation": (
                f"Delivered {days_since} days ago; the window for '{reason}' is "
                f"{window} days. This return cannot be approved automatically. "
                "Say so plainly, do not imply an exception may be possible, and "
                "offer to pass it to a human who can review a goodwill exception."
            ),
        }

    return {
        "eligible": True,
        "code": "approved",
        "window_days": window,
        "days_since_delivery": days_since,
        "refund_amount": money(order["total_cents"]),
        "return_shipping": "free" if reason in EXTENDED_WINDOW_REASONS else "prepaid label, $4.99 deducted",
    }


def check_return_eligibility(ctx: ToolContext, order_number: str, reason: str) -> dict:
    customer_id = customer_id_from(ctx.token, "orders:read")
    order = db.get_order(customer_id, order_number)
    if order is None:
        return {"found": False, "note": f"No order {order_number} on this account."}
    return {"found": True, "order_number": order["order_number"], **evaluate(order, reason)}


def create_return(ctx: ToolContext, order_number: str, reason: str) -> dict:
    """Note that this re-runs ``evaluate`` rather than trusting that the model
    called ``check_return_eligibility`` first and read the answer correctly.
    The write path validates its own preconditions."""
    customer_id = customer_id_from(ctx.token, "returns:write")
    order = db.get_order(customer_id, order_number)
    if order is None:
        return {"created": False, "note": f"No order {order_number} on this account."}

    verdict = evaluate(order, reason)
    if not verdict["eligible"]:
        return {
            "created": False,
            "refused_by": "policy_engine",
            **verdict,
            "note": "The return was NOT created. Tell the customer why, using the explanation.",
        }

    rma = f"RMA-{order['order_number'].split('-')[-1]}-{secrets.token_hex(2).upper()}"
    row = db.insert_return(order["id"], rma, reason, order["total_cents"])

    # The customer leaves with the RMA in writing, not just on screen. Same
    # rule as the handoff: the address comes from the verified account.
    delivery = _email_rma(ctx, order, row, verdict)

    return {
        "created": True,
        "rma": row["rma"],
        "refund_amount": money(row["refund_cents"]),
        "return_shipping": verdict["return_shipping"],
        "email_sent": bool(delivery),
        "emailed_to": delivery["delivered_to"] if delivery else None,
        "next_step": (
            f"Confirmation and a prepaid label emailed to {delivery['delivered_to']}. "
            "Refund lands 5-7 business days after we receive the book."
            if delivery else
            "Refund lands 5-7 business days after we receive the book. Do NOT claim "
            "anything was emailed."
        ),
    }


def _email_rma(ctx: ToolContext, order: dict, row: dict, verdict: dict) -> dict | None:
    try:
        customer_id = customer_id_from(ctx.token, "notifications:send")
        customer = db.get_customer(customer_id)
        if not customer:
            return None
        titles = "\n".join(f"  - {i['title']} by {i['author']}" for i in order["items"])
        email = notifications.Email(
            to=notifications.resolve_recipient(customer["email"]),
            subject=f"Bookly return {row['rma']} — your prepaid label",
            body=f"""Hi {customer['full_name'].split()[0]},

Your return is set up. Here is everything you need.

Return number: {row['rma']}
Order:         {order['order_number']}
Refund:        {money(row['refund_cents'])} to your original payment method
Postage:       {verdict['return_shipping'].capitalize()}

Items coming back to us
{titles}

What happens next
  1. Print your prepaid label: bookly.example/returns/{row['rma']}
  2. Drop the parcel at any post office within 14 days.
  3. We refund within 5-7 business days of it reaching us.

Reply to this email if anything looks wrong.

— Bookly Support
""",
        )
        record = notifications.outbox().send(email)
        return {"delivered_to": notifications.mask(email.to), "transport": record["transport"]}
    except (AuthorizationError, Exception) as exc:  # noqa: BLE001
        # The return is already written. A mail failure must not undo it.
        log.error("RMA email failed for %s: %s", row["rma"], exc)
        return None


_reason_prop = {
    "type": "string",
    "enum": REASONS,
    "description": "Why the customer wants to return the item. Ask them if unclear; do not guess.",
}

TOOLS = [
    Tool(
        name="check_return_eligibility",
        description=(
            "Check whether one of the verified customer's orders can be returned "
            "for a given reason. Always call this before telling a customer "
            "whether a return is possible — the return window depends on the "
            "reason and on the delivery date."
        ),
        input_schema=obj(
            {"order_number": {"type": "string"}, "reason": _reason_prop},
            ["order_number", "reason"],
        ),
        handler=check_return_eligibility,
        required_scope="orders:read",
        tags=["returns"],
    ),
    Tool(
        name="create_return",
        description=(
            "Open a return and issue an RMA number for one of the verified "
            "customer's orders. This is a write action: only call it after the "
            "customer has explicitly confirmed they want to go ahead."
        ),
        input_schema=obj(
            {"order_number": {"type": "string"}, "reason": _reason_prop},
            ["order_number", "reason"],
        ),
        handler=create_return,
        required_scope="returns:write",
        tags=["returns", "write"],
    ),
]
