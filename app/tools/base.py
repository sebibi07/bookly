"""Tool contract.

Note what is *not* in ``input_schema`` anywhere in this package: a customer
identifier. The model cannot pass one because no tool accepts one. Identity
enters through ``ToolContext.token`` -- which the model never sees -- and is
decoded server-side on every call.
"""
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ToolContext:
    """Server-side state handed to a handler. Never serialised to the model."""
    token: str | None = None
    session: Any = None


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Callable[..., dict]
    # None means the tool is callable by an unverified visitor.
    required_scope: str | None = None
    # Marks tools whose results should be surfaced in the transcript UI.
    tags: list[str] = field(default_factory=list)

    def schema(self) -> dict:
        """The shape the Messages API receives. ``strict`` guarantees the
        arguments validate, so handlers never defend against malformed input."""
        return {
            "name": self.name,
            "description": self.description,
            "strict": True,
            "input_schema": self.input_schema,
        }


def obj(props: dict, required: list[str]) -> dict:
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


def money(cents: int) -> str:
    return f"${cents / 100:,.2f}"
