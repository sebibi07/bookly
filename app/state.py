"""Conversation state.

Deliberately *slots*, not stages. A customer who opens with "where's BK-10021,
I'm sarah.chen@example.com, 94110" has filled three slots in one breath; a
stage machine would still march them through three questions. What the agent
asks next is derived from what is missing, never from a step counter.
"""
import secrets
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AuthState:
    verified: bool = False
    # The token never leaves the server and is never placed in ``messages``.
    token: str | None = None
    customer_name: str | None = None
    attempts: int = 0
    locked: bool = False

    def public(self) -> dict:
        """What the trace and the UI may see. Note the absence of the token."""
        return {
            "verified": self.verified,
            "customer_name": self.customer_name,
            "failed_attempts": self.attempts,
            "locked": self.locked,
        }


@dataclass
class Session:
    session_id: str = field(default_factory=lambda: secrets.token_urlsafe(8))
    # Anthropic-format message history. Tool results live here too.
    messages: list[dict] = field(default_factory=list)
    auth: AuthState = field(default_factory=AuthState)
    # Last classified intent. Sticky: a follow-up like "and where is it?" keeps
    # the previous intent rather than being reclassified as unclear.
    intent: str = "unclear"
    escalated: bool = False
    escalation: dict | None = None
    trace: list[dict] = field(default_factory=list)

    def turn_count(self) -> int:
        return sum(1 for m in self.messages if m["role"] == "user" and isinstance(m["content"], str))


class SessionStore:
    """In-memory sessions. Redis in production; a dict is honest for a demo."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def get_or_create(self, session_id: str | None) -> Session:
        if session_id and session_id in self._sessions:
            return self._sessions[session_id]
        session = Session()
        self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def reset(self, session_id: str) -> Session:
        self._sessions.pop(session_id, None)
        session = Session()
        self._sessions[session.session_id] = session
        return session


store = SessionStore()


def redact(value: Any) -> Any:
    """Used before anything goes into a trace or a log line."""
    if isinstance(value, dict):
        return {
            k: ("***" if k in {"token", "shipping_zip", "email"} else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value
