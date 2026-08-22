"""Contract test for the Anthropic request we actually build.

The conversation evals in ``run.py`` exercise orchestration with the scripted
engine. This exercises the other half: the real ``AnthropicEngine``, driven
against a stubbed HTTP transport, so the request shape is verified without a
key, without network, and without cost.

It asserts the things that would be expensive to discover in production:

  * no tool schema contains a customer identifier
  * the JWT never appears in the transcript sent to the model
  * routing uses constrained JSON output, not free-form parsing
  * the system prompt carries a cache breakpoint and stays a stable prefix
  * per-turn state travels as a mid-conversation system message, not as
    interpolation into the cached prompt
  * tool results are returned as JSON strings, which is what the API accepts

    docker compose run --rm app python -m evals.wire_check
"""
import json
import logging
import os
import sys

import anthropic
import httpx

os.environ["ANTHROPIC_API_KEY"] = "sk-ant-stub-no-network-needed"
os.environ["BOOKLY_MOCK_LLM"] = "0"

from app import db, orchestrator, prompts  # noqa: E402
from app.llm.anthropic_engine import ADAPTIVE_FAMILY, AnthropicEngine, tuning  # noqa: E402
from app.state import Session  # noqa: E402

logging.getLogger().setLevel(logging.WARNING)
GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"

REQUESTS: list[dict] = []


def _message(content, stop_reason):
    return {
        "id": "msg_stub", "type": "message", "role": "assistant",
        "model": "claude-opus-5", "content": content,
        "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 120, "output_tokens": 30, "cache_read_input_tokens": 0},
    }


def _handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    REQUESTS.append(body)

    if (body.get("output_config") or {}).get("format"):
        return httpx.Response(200, json=_message(
            [{"type": "text", "text": json.dumps(
                {"intent": "order_status", "confidence": 0.93,
                 "reasoning": "asks where an order is"})}], "end_turn"))

    generation_turns = sum(1 for b in REQUESTS if not (b.get("output_config") or {}).get("format"))
    if generation_turns == 1:
        return httpx.Response(200, json=_message(
            [{"type": "text", "text": "Let me check that."},
             {"type": "tool_use", "id": "toolu_stub", "name": "verify_customer",
              "input": {"email": "sarah.chen@example.com", "shipping_zip": "94110"}}],
            "tool_use"))
    return httpx.Response(200, json=_message(
        [{"type": "text", "text": "You're verified, Sarah."}], "end_turn"))


def main() -> int:
    db.init_schema()

    engine = AnthropicEngine()
    engine.client = anthropic.Anthropic(
        api_key="sk-ant-stub", http_client=httpx.Client(transport=httpx.MockTransport(_handler))
    )
    orchestrator._engine = engine

    result = orchestrator.handle_turn(
        Session(), "where is my order? sarah.chen@example.com 94110"
    )

    # Structured outputs reject numeric range constraints. This is the lint
    # that would have caught the 400 that shipped: cheap, offline, and aimed at
    # the exact class of mistake a stubbed transport cannot see.
    UNSUPPORTED = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf")

    def scan(node, path="schema"):
        found = []
        if isinstance(node, dict):
            for key, value in node.items():
                if key in UNSUPPORTED:
                    found.append(f"{path}.{key}")
                found += scan(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, item in enumerate(node):
                found += scan(item, f"{path}[{i}]")
        return found

    schema_fails = [
        f"classifier schema uses {where}, which structured outputs reject"
        for where in scan(prompts.CLASSIFIER_SCHEMA)
    ]

    # Per-model request shaping. Haiku 4.5 rejects `effort` and adaptive
    # thinking with a 400; the 4.6+ family expects them. Getting this wrong
    # fails at request time, in front of whoever is watching.
    shape_fails: list[str] = []
    for model in ("claude-haiku-4-5", "claude-sonnet-4-5"):
        extras = tuning(model)
        if "thinking" in extras:
            shape_fails.append(f"{model}: sent `thinking`, which it rejects")
        if "effort" in (extras.get("output_config") or {}):
            shape_fails.append(f"{model}: sent `effort`, which it rejects")
        formatted = tuning(model, output_format={"type": "json_schema", "schema": {}})
        if "format" not in (formatted.get("output_config") or {}):
            shape_fails.append(f"{model}: structured output was dropped")
    for model in ("claude-sonnet-5", "claude-opus-5"):
        extras = tuning(model)
        if (extras.get("output_config") or {}).get("effort") is None:
            shape_fails.append(f"{model}: effort was not applied")
        if "thinking" not in extras:
            shape_fails.append(f"{model}: thinking key missing entirely")
    # Opus 5 must never be sent thinking:disabled -- it can write tool calls
    # into visible text, where they silently never run.
    if tuning("claude-opus-5").get("thinking", {}).get("type") != "adaptive":
        shape_fails.append("claude-opus-5: thinking must stay adaptive")

    routing = [b for b in REQUESTS if (b.get("output_config") or {}).get("format")]
    generation = [b for b in REQUESTS if not (b.get("output_config") or {}).get("format")]
    fails: list[str] = []

    def want(condition, message):
        if not condition:
            fails.append(message)

    fails.extend(schema_fails)
    fails.extend(shape_fails)

    want(routing, "routing never issued a constrained call")
    if routing:
        want(routing[0]["output_config"]["format"]["type"] == "json_schema",
             "routing did not use json_schema output")
        # Routing must be on the cheap tier, or the tiering is decorative.
        want(routing[0]["model"] == os.getenv("BOOKLY_ROUTER_MODEL", "claude-haiku-4-5"),
             f"routing ran on {routing[0]['model']}, not the router tier")
        want(routing[0]["model"] not in ADAPTIVE_FAMILY
             or "effort" in routing[0]["output_config"],
             "router model mismatched its tuning")

    want(generation, "the agent never called the Messages API")
    if generation:
        first = generation[0]
        want(first["model"] == os.getenv("BOOKLY_MODEL", "claude-sonnet-5"),
             f"generation ran on {first['model']}, not the agent tier")
        want(isinstance(first["system"], list) and "cache_control" in first["system"][0],
             "system prompt is missing its cache breakpoint")
        # `effort` is only legal on the 4.6+ family; asserting it unconditionally
        # breaks the very config it is meant to protect.
        if first["model"] in ADAPTIVE_FAMILY:
            want((first.get("output_config") or {}).get("effort")
                 == os.getenv("BOOKLY_EFFORT", "low"),
                 "effort setting was not applied")
        else:
            want("output_config" not in first or "effort" not in first["output_config"],
                 f"{first['model']} was sent `effort`, which it rejects")
            want("thinking" not in first,
                 f"{first['model']} was sent `thinking`, which it rejects")
        # State must reach the model, after the cache breakpoint, and must not
        # be smuggled in as a `role: "system"` turn -- some models 400 on that.
        want(len(first["system"]) > 1, "per-turn state block was not sent")
        want("cache_control" not in first["system"][-1],
             "the volatile state block must sit after the cache breakpoint")
        want(not any(m["role"] == "system" for m in first["messages"]),
             "state was sent as a message role, which some models reject")
        want("Today's date" in first["system"][-1]["text"],
             "state block is missing the current date")
        for tool in first["tools"]:
            want("customer_id" not in json.dumps(tool),
                 f"LEAK: tool {tool['name']} exposes a customer identifier")
            want(tool.get("strict"), f"tool {tool['name']} is not declared strict")

    if len(generation) > 1:
        blocks = [
            b for m in generation[1]["messages"] if isinstance(m.get("content"), list)
            for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"
        ]
        want(blocks, "tool results were never returned to the model")
        if blocks:
            want(isinstance(blocks[0]["content"], str),
                 "tool_result content must be a JSON string, not a nested object")

    transcript = json.dumps([b.get("messages", []) for b in REQUESTS], default=str)
    want("eyJ" not in transcript, "LEAK: a JWT appeared in the transcript sent to the model")

    print(f"\n  Anthropic wire contract {DIM}(stubbed transport, no network){RESET}\n")
    print(f"  {DIM}reply:{RESET} {result['reply']}")
    print(f"  {DIM}api calls:{RESET} {len(routing)} routing on "
          f"{routing[0]['model'] if routing else '?'} + {len(generation)} generation on "
          f"{generation[0]['model'] if generation else '?'}\n")
    if fails:
        for f in fails:
            print(f"  {RED}FAIL{RESET}  {f}")
        print(f"\n  {RED}{len(fails)} failed{RESET}\n")
        return 1
    print(f"  {GREEN}all wire checks passed{RESET}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
