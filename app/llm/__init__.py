"""LLM boundary.

One narrow interface with two implementations. The real one talks to the
Messages API; the mock is a deterministic scripted engine. The orchestrator
cannot tell them apart, which is what lets `docker compose up` work with no
API key and lets the eval suite run in CI without spending money.
"""
from dataclasses import dataclass, field

from app import config


@dataclass
class LLMResult:
    """Normalised model turn. ``content`` is always a list of plain dicts in
    Anthropic content-block shape."""
    content: list[dict]
    stop_reason: str = "end_turn"
    usage: dict = field(default_factory=dict)
    latency_ms: float = 0.0
    refused: bool = False

    def tool_calls(self) -> list[dict]:
        return [b for b in self.content if b.get("type") == "tool_use"]

    def text(self) -> str:
        return "\n".join(b["text"] for b in self.content if b.get("type") == "text").strip()


def get_engine():
    if config.USE_MOCK_LLM:
        from app.llm.mock import MockEngine

        return MockEngine()
    from app.llm.anthropic_engine import AnthropicEngine

    return AnthropicEngine()
