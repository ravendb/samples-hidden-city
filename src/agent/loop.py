"""
OpenAI tool-calling loop.

Budget: ~1500 tokens total per turn (system 200 + user 100 + tool results 800 + response 400).
The loop logs actual usage from the API response so measure_tokens.py can track it.
"""
import json
import logging
import os
from dataclasses import dataclass, field
from typing import AsyncGenerator

from openai import AsyncOpenAI

from src.agent.prompts import SYSTEM_PROMPT
from src.tools.definitions import TOOL_DEFINITIONS
from src.tools.dispatcher import dispatch_tool

log = logging.getLogger(__name__)

_DEFAULT_MODEL = "gpt-4o-mini"
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
    """Rough estimate: 1 token ~= 4 chars. Used for budget warnings only."""
    return len(text) // 4


async def run_agent(
    user_message: str,
    prior_turns: list[dict],
) -> AgentResult:
    """
    prior_turns: list of {"role": ..., "content": ...} built from the session document.
    Callers (app.py) are responsible for loading the session from RavenDB first.
    """
    client = AsyncOpenAI()
    messages = (
        [{"role": "system", "content": SYSTEM_PROMPT}]
        + prior_turns
        + [{"role": "user", "content": user_message}]
    )

    total_input = 0
    total_output = 0
    tool_calls = 0
    token_warnings: list[str] = []

    for iteration in range(1, MAX_ITERATIONS + 1):
        response = await client.chat.completions.create(
            model=os.getenv("LLM_MODEL", _DEFAULT_MODEL),
            max_tokens=MAX_RESPONSE_TOKENS,
            tools=TOOL_DEFINITIONS,
            messages=messages,
        )

        total_input += response.usage.prompt_tokens
        total_output += response.usage.completion_tokens
        choice = response.choices[0]
        log.info(
            "Iteration %d -- stop=%s in=%d out=%d",
            iteration,
            choice.finish_reason,
            response.usage.prompt_tokens,
            response.usage.completion_tokens,
        )

        if choice.finish_reason == "stop":
            text = choice.message.content
            if text is None:
                raise ValueError("OpenAI returned stop with no text content")
            return AgentResult(
                response=text,
                input_tokens=total_input,
                output_tokens=total_output,
                tool_calls=tool_calls,
                iterations=iteration,
                tool_token_warnings=token_warnings,
            )

        if choice.finish_reason == "tool_calls":
            assistant_message = {
                "role": "assistant",
                "content": choice.message.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in (choice.message.tool_calls or [])
                ],
            }
            tool_results = []

            for tc in choice.message.tool_calls or []:
                tool_calls += 1
                tool_input = json.loads(tc.function.arguments)
                result = await dispatch_tool(tc.function.name, tool_input)
                result_json = json.dumps(result, default=str)

                estimated = _estimate_tokens(result_json)
                if estimated > WARN_TOOL_TOKENS:
                    warning = f"{tc.function.name} result ~{estimated} tokens (budget {WARN_TOOL_TOKENS})"
                    log.warning(warning)
                    token_warnings.append(warning)

                tool_results.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result_json,
                })

            messages = messages + [assistant_message] + tool_results

    raise RuntimeError(f"Agent exceeded {MAX_ITERATIONS} iterations without finishing")


async def stream_agent(
    user_message: str,
    prior_turns: list[dict],
) -> AsyncGenerator[str, None]:
    """Streaming variant -- yields SSE-formatted strings for /chat/stream."""
    client = AsyncOpenAI()
    messages = (
        [{"role": "system", "content": SYSTEM_PROMPT}]
        + prior_turns
        + [{"role": "user", "content": user_message}]
    )
    total_input, total_output, tool_calls_count = 0, 0, 0

    def sse(data: dict) -> str:
        return f"data: {json.dumps(data)}\n\n"

    for _ in range(MAX_ITERATIONS):
        accumulated_content: list[str] = []
        accumulated_tool_calls: dict[int, dict] = {}
        finish_reason = None

        stream = await client.chat.completions.create(
            model=os.getenv("LLM_MODEL", _DEFAULT_MODEL),
            max_tokens=MAX_RESPONSE_TOKENS,
            messages=messages,
            tools=TOOL_DEFINITIONS,
            stream=True,
            stream_options={"include_usage": True},
        )

        async for chunk in stream:
            if chunk.usage:
                total_input += chunk.usage.prompt_tokens
                total_output += chunk.usage.completion_tokens
                continue
            if not chunk.choices:
                continue
            c = chunk.choices[0]
            if c.finish_reason:
                finish_reason = c.finish_reason
            delta = c.delta
            if delta.content:
                accumulated_content.append(delta.content)
                yield sse({"type": "delta", "text": delta.content})
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in accumulated_tool_calls:
                        accumulated_tool_calls[idx] = {"id": "", "name": "", "arguments": ""}
                    if tc_delta.id:
                        accumulated_tool_calls[idx]["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            accumulated_tool_calls[idx]["name"] += tc_delta.function.name
                        if tc_delta.function.arguments:
                            accumulated_tool_calls[idx]["arguments"] += tc_delta.function.arguments

        if finish_reason == "stop":
            yield sse({
                "type": "done",
                "input_tokens": total_input,
                "output_tokens": total_output,
                "total_tokens": total_input + total_output,
                "tool_calls": tool_calls_count,
            })
            return

        if finish_reason == "tool_calls":
            tool_results: list[dict] = []
            assistant_tool_calls = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": tc["arguments"]},
                }
                for tc in accumulated_tool_calls.values()
            ]
            assistant_message = {
                "role": "assistant",
                "content": "".join(accumulated_content) or None,
                "tool_calls": assistant_tool_calls,
            }

            for tc in accumulated_tool_calls.values():
                tool_calls_count += 1
                yield sse({"type": "tool_call", "name": tc["name"]})
                tool_input = json.loads(tc["arguments"])
                result = await dispatch_tool(tc["name"], tool_input)
                result_json = json.dumps(result, default=str)
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result_json,
                })

            messages = messages + [assistant_message] + tool_results

    yield sse({"type": "error", "message": f"Agent exceeded {MAX_ITERATIONS} iterations"})
