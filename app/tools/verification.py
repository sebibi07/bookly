"""The verification tool -- the hinge between step 2 and step 3 of the agent.

The model's only power here is to *submit* two values the customer gave it.
It does not perform the comparison, does not see the stored ZIP, and does not
decide the outcome. It receives a boolean.
"""
import logging

from app import config, identity
from app.tools.base import Tool, ToolContext, obj

log = logging.getLogger("bookly.tools.verification")


def verify_customer(ctx: ToolContext, email: str, shipping_zip: str) -> dict:
    session = ctx.session

    if session.auth.locked:
        return {
            "verified": False,
            "locked": True,
            "note": (
                "Verification is locked after too many failed attempts. Do not "
                "accept further attempts. Offer to transfer to a human agent."
            ),
        }

    result = identity.verify_and_issue(email, shipping_zip)

    if not result.ok:
        session.auth.attempts += 1
        remaining = config.MAX_VERIFICATION_ATTEMPTS - session.auth.attempts
        if remaining <= 0:
            session.auth.locked = True
            return {
                "verified": False,
                "locked": True,
                "note": (
                    "Too many failed attempts; verification is now locked for this "
                    "conversation. Apologise and offer a human agent."
                ),
            }
        return {
            "verified": False,
            "attempts_remaining": remaining,
            "note": (
                "Those details do not match an account. Do NOT say which of the "
                "two was wrong — that would leak whether the email is registered. "
                "Ask the customer to check both and try again."
            ),
        }

    # Success: the token goes into server-side session state. It is never put
    # into the transcript, so it cannot be echoed back out by the model.
    session.auth.token = result.token
    session.auth.verified = True
    session.auth.customer_name = result.customer_name
    log.info("session %s verified", session.session_id)
    return {
        "verified": True,
        "customer_name": result.customer_name,
        "note": (
            f"Verified. Greet {result.customer_name} by first name once, then "
            "continue with what they originally asked for — do not make them "
            "repeat it."
        ),
    }


TOOLS = [
    Tool(
        name="verify_customer",
        description=(
            "Verify the customer's identity using their email address and the ZIP "
            "code their order shipped to. Call this only once you have collected "
            "BOTH values from the customer in the conversation. Never invent, "
            "guess, or auto-fill either value."
        ),
        input_schema=obj(
            {
                "email": {"type": "string", "description": "Exactly as the customer typed it."},
                "shipping_zip": {"type": "string", "description": "Exactly as the customer typed it."},
            },
            ["email", "shipping_zip"],
        ),
        handler=verify_customer,
        required_scope=None,
        tags=["auth"],
    )
]
