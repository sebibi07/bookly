"""Escalation.

Deflection rate is a vanity metric if the contained conversations were bad
ones. This tool is what makes 'the agent knows when to stop' a capability
rather than a failure: the human receives verified identity, the original
question, and every tool result the agent already gathered, so the customer
never repeats themselves.
"""
import logging
import secrets
from datetime import datetime, timezone

from app import db, notifications
from app.identity import AuthorizationError, customer_id_from
from app.tools.base import Tool, ToolContext, obj

log = logging.getLogger("bookly.tools.handoff")

REASON_CODES = [
    "policy_exception_requested",
    "carrier_trace_required",
    "identity_verification_failed",
    "out_of_scope",
    "customer_requested_human",
    "agent_uncertain",
]


# Customers should never read our internal tool names. This is the difference
# between an artefact and a debug dump.
STEP_PHRASING = {
    "verify_customer": "Confirmed your identity",
    "list_my_orders": "Looked up the orders on your account",
    "get_order_status": "Checked where your parcel is",
    "check_return_eligibility": "Checked your return against our policy",
    "create_return": "Started your return",
    "search_help_center": "Checked our published policy",
}

REASON_HEADLINES = {
    "policy_exception_requested": "your return request",
    "carrier_trace_required": "your missing parcel",
    "identity_verification_failed": "verifying your account",
    "out_of_scope": "your enquiry",
    "customer_requested_human": "your enquiry",
    "agent_uncertain": "your enquiry",
}


def _compose(ticket: str, name: str, reason_code: str, summary: str,
             steps: list[str]) -> notifications.Email:
    topic = REASON_HEADLINES.get(reason_code, "your enquiry")
    checked = "\n".join(f"  - {s}" for s in steps) or "  - (nothing yet)"
    return notifications.Email(
        to="",  # filled in by the caller from the verified account
        subject=f"Bookly support — reference {ticket}",
        body=f"""Hi {name.split()[0]},

Thanks for chatting with us about {topic}. One of our team is picking this up
now, and this email is your record of it.

Your reference: {ticket}

What you asked us about
{summary}

What we already checked for you
{checked}

You will not need to repeat any of this — the colleague taking over can see the
whole conversation and everything above. They will reply to this address.

If anything here looks wrong, just reply and say so.

— Bookly Support
Raised {datetime.now(timezone.utc).strftime('%d %B %Y at %H:%M UTC')}
""",
    )


def _notify(ctx: ToolContext, ticket: str, reason_code: str, summary: str,
            steps: list[str]) -> dict | None:
    """Mail the customer their handoff record.

    Returns None when there is nobody we can legitimately write to. The address
    comes from the verified account -- never from the conversation -- so an
    unverified session sends nothing, by design.
    """
    session = ctx.session
    if not session.auth.verified:
        log.info("no email sent for %s: session is unverified", ticket)
        return None
    try:
        customer_id = customer_id_from(ctx.token, "notifications:send")
    except AuthorizationError as exc:
        log.warning("no email sent for %s: %s", ticket, exc)
        return None

    customer = db.get_customer(customer_id)
    if not customer:
        return None

    email = _compose(ticket, customer["full_name"], reason_code, summary, steps)
    email.to = notifications.resolve_recipient(customer["email"])
    try:
        record = notifications.outbox().send(email)
    except Exception as exc:  # noqa: BLE001
        # Never let a mail problem break the handoff itself.
        log.error("handoff email failed for %s: %s", ticket, exc)
        return None
    return {
        "account_email": notifications.mask(customer["email"]),
        "delivered_to": notifications.mask(email.to),
        "transport": record["transport"],
        "redirected": bool(email.to != customer["email"]),
    }


def escalate_to_human(ctx: ToolContext, reason_code: str, summary: str) -> dict:
    session = ctx.session
    ticket = f"ESC-{secrets.token_hex(3).upper()}"
    session.escalated = True
    session.escalation = {
        "ticket": ticket,
        "reason_code": reason_code,
        "summary": summary,
        "verified": session.auth.verified,
        "customer_name": session.auth.customer_name,
        # The receiving human gets the agent's actual working, not a paraphrase.
        "tools_already_run": [
            {"tool": c["tool"], "result": c["result"]}
            for turn in session.trace
            for c in turn["tool_calls"]
        ],
        "transcript_turns": len([m for m in session.messages if m["role"] == "user"]),
    }
    # Fired here, not exposed as a tool: mailing the customer their record is a
    # business process that must always happen, not a judgement call the model
    # gets to forget.
    steps, seen = [], set()
    for turn in session.trace:
        for call in turn["tool_calls"]:
            phrase = STEP_PHRASING.get(call["tool"])
            if phrase and not call["error"] and phrase not in seen:
                seen.add(phrase)
                steps.append(phrase)
    delivery = _notify(ctx, ticket, reason_code, summary, steps)
    session.escalation["email"] = delivery

    if delivery:
        note = (
            f"Tell the customer their reference is {ticket}, that you have emailed "
            f"a copy to {delivery['delivered_to']}, and that a human has the full "
            "history so they will not need to repeat themselves. Give the wait "
            "estimate, then stop — do not keep troubleshooting."
        )
    else:
        note = (
            f"Tell the customer their reference is {ticket} and to keep it safe. "
            "Do NOT claim you have emailed anything — their identity is not "
            "verified, so no email was sent. Give the wait estimate, then stop."
        )

    return {
        "escalated": True,
        "ticket": ticket,
        "queue_wait_estimate": "about 4 minutes",
        "email_sent": bool(delivery),
        "emailed_to": delivery["delivered_to"] if delivery else None,
        "note": note,
    }


TOOLS = [
    Tool(
        name="escalate_to_human",
        description=(
            "Hand the conversation to a human support agent. Call this when: the "
            "customer asks for a person; a tool result says human review is "
            "required; identity verification is locked; a policy exception is "
            "being requested; or you do not have a tool that can answer and are "
            "about to guess. Prefer escalating over improvising."
        ),
        input_schema=obj(
            {
                "reason_code": {"type": "string", "enum": REASON_CODES},
                "summary": {
                    "type": "string",
                    "description": "2-3 sentences for the human: what the customer wants and what you already established.",
                },
            },
            ["reason_code", "summary"],
        ),
        handler=escalate_to_human,
        required_scope=None,
        tags=["handoff"],
    )
]
