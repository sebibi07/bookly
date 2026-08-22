"""Identity service: verify a human, then mint a scoped, short-lived token.

This is the security spine of the demo. Two properties matter:

1. **The model is never the security boundary.** Verification is a
   constant-time comparison in Python against Postgres. The LLM is not asked
   whether two ZIP codes look close enough; it never sees the stored value at
   all. It only learns pass/fail.

2. **The model cannot name a customer.** Tools do not accept a ``customer_id``
   parameter -- it is not in any schema the model receives. Every scoped read
   derives the customer from the ``sub`` claim of this token, server-side. A
   fully jailbroken model still cannot address another customer's data,
   because there is no argument through which to ask for it.
"""
import hmac
import logging
import secrets
import time
from dataclasses import dataclass

import jwt

from app import config, db

log = logging.getLogger("bookly.identity")


class AuthorizationError(Exception):
    """Raised when a tool is invoked without a token that permits it."""


@dataclass
class VerificationResult:
    ok: bool
    token: str | None = None
    customer_name: str | None = None
    failure_reason: str | None = None


# Scopes a successful chat verification is allowed to receive. Intentionally
# narrow: this token can read orders, open a return, and have Bookly mail the
# account holder. It cannot change an address, cannot touch payment methods,
# cannot read another customer, and cannot nominate a different recipient.
CHAT_SCOPES = ["orders:read", "returns:write", "notifications:send"]


def verify_and_issue(email: str, shipping_zip: str) -> VerificationResult:
    """Two-factor check: something the customer states (email) plus something
    only the account holder should know (the ZIP the order shipped to)."""
    customer = db.find_customer_by_email(email)
    if customer is None:
        # Same generic outcome as a ZIP mismatch. Distinguishing them would
        # turn this endpoint into an account-enumeration oracle.
        log.info("verification failed: no such email")
        return VerificationResult(ok=False, failure_reason="no_match")

    supplied = "".join(ch for ch in shipping_zip if ch.isalnum()).upper()
    stored = "".join(ch for ch in customer["shipping_zip"] if ch.isalnum()).upper()
    if not hmac.compare_digest(supplied, stored):
        log.info("verification failed: zip mismatch for customer_id=%s", customer["id"])
        return VerificationResult(ok=False, failure_reason="no_match")

    now = int(time.time())
    claims = {
        "iss": config.JWT_ISSUER,
        "aud": config.JWT_AUDIENCE,
        "sub": str(customer["id"]),
        "scope": CHAT_SCOPES,
        "iat": now,
        "exp": now + config.TOKEN_TTL_SECONDS,
        "jti": secrets.token_urlsafe(12),
        # Recorded so an auditor can answer "how was this person verified?"
        "amr": ["email", "shipping_zip"],
    }
    token = jwt.encode(claims, config.JWT_SECRET, algorithm="HS256")
    log.info("issued token jti=%s sub=%s scopes=%s", claims["jti"], claims["sub"], CHAT_SCOPES)
    return VerificationResult(ok=True, token=token, customer_name=customer["full_name"])


def authorize(token: str | None, required_scope: str) -> dict:
    """Validate a token and assert a scope. Raises on every failure path."""
    if not token:
        raise AuthorizationError("no_token")
    try:
        claims = jwt.decode(
            token,
            config.JWT_SECRET,
            algorithms=["HS256"],
            audience=config.JWT_AUDIENCE,
            issuer=config.JWT_ISSUER,
        )
    except jwt.ExpiredSignatureError:
        raise AuthorizationError("token_expired")
    except jwt.InvalidTokenError as exc:
        raise AuthorizationError(f"invalid_token:{exc}")

    if required_scope not in claims.get("scope", []):
        raise AuthorizationError(f"missing_scope:{required_scope}")
    return claims


def customer_id_from(token: str | None, required_scope: str) -> int:
    return int(authorize(token, required_scope)["sub"])
