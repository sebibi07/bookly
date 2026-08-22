"""Anthropic Messages API engine.

The agentic loop is written by hand in ``app/orchestrator.py`` rather than
handed to the SDK's tool runner. That is deliberate: the loop needs to
recompute the tool list from auth state *between* iterations, which is exactly
the seam a turnkey runner hides.
"""
import json
import logging
import time

import anthropic

from app import config, prompts

log = logging.getLogger("bookly.llm")

# The 4.6-and-later family accepts `output_config.effort` and adaptive
# thinking. Older models -- Haiku 4.5 among them -- reject `effort` with a 400
# and use the deprecated budget_tokens form of thinking, which we do not want
# in a latency-sensitive chat anyway. Sending the wrong shape is a hard error,
# not a silent downgrade, so the tuning is chosen per model rather than assumed.
ADAPTIVE_FAMILY = {
    "claude-fable-5", "claude-mythos-5",
    "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
    "claude-sonnet-5", "claude-sonnet-4-6",
}


def tuning(model: str, output_format: dict | None = None) -> dict:
    """Per-model request extras, tuned for latency."""
    extras: dict = {}
    output_config: dict = {}

    if model in ADAPTIVE_FAMILY:
        output_config["effort"] = config.EFFORT
        if config.THINKING == "adaptive":
            extras["thinking"] = {"type": "adaptive"}
        elif model == "claude-opus-5":
            # Opus 5 has documented failure modes with thinking switched off --
            # it can write a tool call into visible text, where it never runs.
            # Low effort is the supported way to make it cheap instead.
            extras["thinking"] = {"type": "adaptive"}
        else:
            extras["thinking"] = {"type": "disabled"}
    # Otherwise (Haiku 4.5 and friends): send neither. That is both the only
    # valid shape and the fastest one available on those models.

    if output_format:
        output_config["format"] = output_format
    if output_config:
        extras["output_config"] = output_config
    return extras


def _block_to_dict(block) -> dict:
    """Blocks go back into ``messages`` verbatim on the next turn. Thinking
    blocks in particular must round-trip unchanged, signature included."""
    return block.model_dump(exclude_none=True)


class AnthropicEngine:
    name = "anthropic"

    def __init__(self, api_key: str | None = None) -> None:
        # An explicit key (supplied through the UI) wins over the environment.
        self.client = anthropic.Anthropic(api_key=api_key or config.ANTHROPIC_API_KEY or None)
        self.model = config.MODEL
        self.router_model = config.ROUTER_MODEL

    def validate(self) -> None:
        """Confirm the credential before we let a customer talk to it.

        Uses the Models endpoint rather than a Messages call: it authenticates
        the key and confirms both models are reachable without generating a
        single token. Raises the SDK's own typed error for the caller to
        translate.
        """
        for model in {self.model, self.router_model}:
            self.client.models.retrieve(model)

    def classify(self, transcript: list[dict]) -> dict:
        """Routing is a classification problem, not a generation problem, so it
        gets its own constrained call rather than being inferred from whatever
        the main model happened to do."""
        started = time.perf_counter()
        response = self.client.messages.create(
            model=self.router_model,
            # A label, a float and one sentence. It does not need more.
            max_tokens=500,
            system=prompts.CLASSIFIER_SYSTEM,
            messages=transcript,
            **tuning(
                self.router_model,
                output_format={"type": "json_schema", "schema": prompts.CLASSIFIER_SCHEMA},
            ),
        )
        text = next(b.text for b in response.content if b.type == "text")
        result = json.loads(text)
        # The schema cannot express a range, so the range is enforced here.
        result["confidence"] = min(1.0, max(0.0, float(result.get("confidence", 0.0))))
        result["latency_ms"] = (time.perf_counter() - started) * 1000
        result["usage"] = {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }
        return result

    def respond(self, messages: list[dict], tools: list[dict],
                state: str | None = None) -> "LLMResult":
        from app.llm import LLMResult

        # Per-turn state rides as a second system block, after the cache
        # breakpoint. It was a mid-conversation `role: "system"` message, which
        # only some models accept -- Haiku 4.5 rejects it with a 400. This
        # shape works everywhere, keeps the volatile half out of the cached
        # prefix, and is equally unreachable from anything the customer types.
        system = [
            {
                "type": "text",
                "text": prompts.SYSTEM,
                # Stable across every turn, so it is worth a cache breakpoint.
                "cache_control": {"type": "ephemeral"},
            }
        ]
        if state:
            system.append({"type": "text", "text": state})

        started = time.perf_counter()
        response = self.client.messages.create(
            model=self.model,
            # The prompt asks for one to three sentences. This is a ceiling for
            # a runaway turn, not a target -- deliberately short output is one
            # of the few good reasons to cap this low.
            max_tokens=2000,
            system=system,
            messages=messages,
            tools=tools,
            **tuning(self.model),
        )
        elapsed = (time.perf_counter() - started) * 1000

        # A safety refusal is a real outcome for a public-facing support bot,
        # not an exception. Treat it as a signal to hand off to a human.
        refused = response.stop_reason == "refusal"
        if refused:
            log.warning(
                "model refused: %s",
                getattr(response.stop_details, "category", None),
            )

        return LLMResult(
            content=[_block_to_dict(b) for b in response.content],
            stop_reason=response.stop_reason,
            usage={
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "cache_read_input_tokens": getattr(
                    response.usage, "cache_read_input_tokens", 0
                ),
            },
            latency_ms=elapsed,
            refused=refused,
        )
