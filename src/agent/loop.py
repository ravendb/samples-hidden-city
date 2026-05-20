"""
Anthropic tool-calling loop.

Budget: ~1500 tokens total per turn (system 200 + user 100 + tool results 800 + response 400).
The loop logs actual usage from the API response so measure_tokens.py can track it.
"""
import json
import logging
import os
from dataclasses import dataclass, field

import anthropic

from src.agent.prompts import SYSTEM_PROMPT
from src.tools.definitions import TOOL_DEFINITIONS
from src.tools.dispatcher import dispatch_tool

log = logging.getLogger(__name__)

MODEL = os.getenv("LLM_MODEL", "claude-sonnet-4-20250514")
MAX_RESPONSE_TOKENS = 400
MAX_ITERATIONS = 10
WARN_TOOL_TOKENS = 800  # log a warning when tool results exceed this


@dataclass
class AgentResult:
    response: str
    input_tokens: int
    output_tokens: int
    tool_calls: int = 0
    iterations: int = 0
    tool_token_warnings: list[str] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def _estimate_tokens(text: str) -> int:
    """Rough estimate: 1 token ≈ 4 chars. Used for budget warnings only."""
    return len(text) // 4


async def run_agent(
    user_message: str,
    prior_turns: list[dict],
) -> AgentResult:
    """
    prior_turns: list of {"role": ..., "content": ...} built from the session document.
    Callers (app.py) are responsible for loading the session from RavenDB first.
    """
    client = anthropic.AsyncAnthropic()
    messages = prior_turns + [{"role": "user", "content": user_message}]

    total_input = 0
    total_output = 0
    tool_calls = 0
    token_warnings: list[str] = []

    for iteration in range(1, MAX_ITERATIONS + 1):
        response = await client.messages.create(
            model=MODEL,
            max_tokens=MAX_RESPONSE_TOKENS,
            system=SYSTEM_PROMPT,
            tools=TOOL_DEFINITIONS,
            messages=messages,
        )

        total_input += response.usage.input_tokens
        total_output += response.usage.output_tokens
        log.info(
            "Iteration %d — stop=%s in=%d out=%d",
            iteration,
            response.stop_reason,
            response.usage.input_tokens,
            response.usage.output_tokens,
        )

        if response.stop_reason == "end_turn":
            text = next(
                (b.text for b in response.content if b.type == "text"),
                None,
            )
            if text is None:
                raise ValueError("Anthropic returned end_turn with no text block")
            return AgentResult(
                response=text,
                input_tokens=total_input,
                output_tokens=total_output,
                tool_calls=tool_calls,
                iterations=iteration,
                tool_token_warnings=token_warnings,
            )

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                tool_calls += 1
                result = await dispatch_tool(block.name, block.input)
                result_json = json.dumps(result, default=str)

                estimated = _estimate_tokens(result_json)
                if estimated > WARN_TOOL_TOKENS:
                    warning = f"{block.name} result ~{estimated} tokens (budget {WARN_TOOL_TOKENS})"
                    log.warning(warning)
                    token_warnings.append(warning)

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_json,
                    }
                )

            messages = messages + [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": tool_results},
            ]

    raise RuntimeError(f"Agent exceeded {MAX_ITERATIONS} iterations without finishing")
