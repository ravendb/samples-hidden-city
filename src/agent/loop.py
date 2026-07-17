"""
OpenAI tool-calling loop.

Budget: ~1500 tokens total per turn (system 200 + user 100 + tool results 800 + response 400).
The loop logs actual usage from the API response so measure_tokens.py can track it.
"""
import json
import logging
import os
import re
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

# Numbers with 2+ digits — catches prices/scores but not stray single digits ("1 stop").
_NUMBER_RE = re.compile(r"\d{2,}(?:[.,]\d+)?")


@dataclass
class AgentResult:
    response: str
    input_tokens: int
    output_tokens: int
    tool_calls: int = 0
    iterations: int = 0
    tool_token_warnings: list[str] = field(default_factory=list)
    grounding_warnings: list[str] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def _estimate_tokens(text: str) -> int:
    """Rough estimate: 1 token ~= 4 chars. Used for budget warnings only."""
    return len(text) // 4


def _truncate_tool_result(result_json: str, max_tokens: int = WARN_TOOL_TOKENS) -> str:
    max_chars = max_tokens * 4
    if len(result_json) <= max_chars:
        return result_json
    return result_json[:max_chars] + ' "…truncated"}'


def _find_ungrounded_numbers(response_text: str, known_data: str) -> list[str]:
    """Flag numbers (likely prices/scores) in the reply that don't appear anywhere in
    the system prompt (preloaded preferences), the user's message, or this turn's tool
    results. A hit doesn't prove hallucination (the model may compute a % savings), but
    it's the cheapest mechanical check for the "never state a price ... not returned by
    a tool" rule in SYSTEM_PROMPT — cheaper than parsing city/IATA names out of prose.
    """
    in_response = set(_NUMBER_RE.findall(response_text))
    in_data = set(_NUMBER_RE.findall(known_data))
    return sorted(in_response - in_data)


def _format_preferences(preferences: dict | None) -> str:
    """Render preloaded profile preferences for the system prompt.

    Only non-empty/non-default fields are included to keep this small — the
    preferences dict itself was already fetched locally from RavenDB (see
    app.py's _load_conversation_context), never from an external call.
    """
    if not preferences:
        return "No saved preferences yet — this looks like a new user."

    lines = []
    if preferences.get("name"):
        lines.append(f"- name: {preferences['name']}")
    if preferences.get("carry_on_only"):
        lines.append("- carry_on_only: true")
    if preferences.get("home_airport"):
        lines.append(f"- home_airport: {preferences['home_airport']}")
    if preferences.get("departure_airports"):
        lines.append(f"- departure_airports: {', '.join(preferences['departure_airports'])}")
    if preferences.get("countries_of_interest"):
        lines.append(f"- countries_of_interest: {', '.join(preferences['countries_of_interest'])}")
    if preferences.get("destinations"):
        lines.append(f"- destinations: {', '.join(preferences['destinations'])}")
    if preferences.get("budget_max"):
        currency = preferences.get("budget_currency") or ""
        lines.append(f"- budget_max: {preferences['budget_max']} {currency}".strip())
    if preferences.get("max_stops") is not None:
        lines.append(f"- max_stops: {preferences['max_stops']}")
    if preferences.get("preferred_airlines"):
        lines.append(f"- preferred_airlines: {', '.join(preferences['preferred_airlines'])}")
    if preferences.get("loyalty_programs"):
        lines.append(f"- loyalty_programs: {', '.join(preferences['loyalty_programs'])}")

    if not lines:
        return "No saved preferences yet — this looks like a new user."
    return "Known preferences for this user (already loaded from RavenDB):\n" + "\n".join(lines)


def _build_system_content(user_id: str, session_id: str, preferences: dict | None) -> str:
    return (
        f"{SYSTEM_PROMPT}\n\n"
        f"Current user_id: {user_id}. Session: {session_id}.\n\n"
        f"{_format_preferences(preferences)}"
    )


async def run_agent(
    user_message: str,
    prior_turns: list[dict],
    user_id: str = "anonymous",
    session_id: str = "1",
    preferences: dict | None = None,
) -> AgentResult:
    """
    prior_turns: list of {"role": ..., "content": ...} built from the session document.
    preferences: profile dict built from the Users document (see app.py's
    _load_conversation_context) — preloaded so search behavior does not depend on
    the model choosing to call get_user_profile.
    Callers (app.py) are responsible for loading both from RavenDB first.
    """
    client = AsyncOpenAI()
    system_content = _build_system_content(user_id, session_id, preferences)
    messages = (
        [{"role": "system", "content": system_content}]
        + prior_turns
        + [{"role": "user", "content": user_message}]
    )

    total_input = 0
    total_output = 0
    tool_calls = 0
    token_warnings: list[str] = []
    grounding_warnings: list[str] = []
    tool_result_texts: list[str] = []

    for iteration in range(1, MAX_ITERATIONS + 1):
        # Force at least one tool call on the first turn — otherwise "auto" lets the
        # model skip search_routes/get_live_price entirely and answer from parametric
        # knowledge, which is the main way ungrounded city names/prices sneak in.
        tool_choice = "required" if iteration == 1 else "auto"
        response = await client.chat.completions.create(
            model=os.getenv("LLM_MODEL", _DEFAULT_MODEL),
            max_tokens=MAX_RESPONSE_TOKENS,
            tools=TOOL_DEFINITIONS,
            tool_choice=tool_choice,
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

            known_data = system_content + user_message + "".join(tool_result_texts)
            ungrounded = _find_ungrounded_numbers(text, known_data)
            if ungrounded:
                warning = f"reply contains numbers not seen in tool output or preferences: {ungrounded}"
                log.warning(warning)
                grounding_warnings.append(warning)

            return AgentResult(
                response=text,
                input_tokens=total_input,
                output_tokens=total_output,
                tool_calls=tool_calls,
                iterations=iteration,
                tool_token_warnings=token_warnings,
                grounding_warnings=grounding_warnings,
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
                result = await dispatch_tool(tc.function.name, tool_input, preferences=preferences)
                result_json = json.dumps(result, default=str)

                estimated = _estimate_tokens(result_json)
                if estimated > WARN_TOOL_TOKENS:
                    warning = f"{tc.function.name} result ~{estimated} tokens (budget {WARN_TOOL_TOKENS})"
                    log.warning(warning)
                    token_warnings.append(warning)
                    result_json = _truncate_tool_result(result_json)

                tool_result_texts.append(result_json)
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
    user_id: str = "anonymous",
    session_id: str = "1",
    preferences: dict | None = None,
) -> AsyncGenerator[str, None]:
    """Streaming variant -- yields SSE-formatted strings for /chat/stream."""
    client = AsyncOpenAI()
    system_content = _build_system_content(user_id, session_id, preferences)
    messages = (
        [{"role": "system", "content": system_content}]
        + prior_turns
        + [{"role": "user", "content": user_message}]
    )
    total_input, total_output, tool_calls_count = 0, 0, 0
    tool_result_texts: list[str] = []

    def sse(data: dict) -> str:
        return f"data: {json.dumps(data)}\n\n"

    for iteration in range(1, MAX_ITERATIONS + 1):
        accumulated_content: list[str] = []
        accumulated_tool_calls: dict[int, dict] = {}
        finish_reason = None

        # Same first-turn grounding guard as run_agent — see comment there.
        tool_choice = "required" if iteration == 1 else "auto"
        stream = await client.chat.completions.create(
            model=os.getenv("LLM_MODEL", _DEFAULT_MODEL),
            max_tokens=MAX_RESPONSE_TOKENS,
            messages=messages,
            tools=TOOL_DEFINITIONS,
            tool_choice=tool_choice,
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
            final_text = "".join(accumulated_content)
            known_data = system_content + user_message + "".join(tool_result_texts)
            ungrounded = _find_ungrounded_numbers(final_text, known_data)
            if ungrounded:
                log.warning(
                    "reply contains numbers not seen in tool output or preferences: %s",
                    ungrounded,
                )

            yield sse({
                "type": "done",
                "input_tokens": total_input,
                "output_tokens": total_output,
                "total_tokens": total_input + total_output,
                "tool_calls": tool_calls_count,
                "grounding_warnings": ungrounded,
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
                result = await dispatch_tool(tc["name"], tool_input, preferences=preferences)
                result_json = json.dumps(result, default=str)
                if _estimate_tokens(result_json) > WARN_TOOL_TOKENS:
                    log.warning("%s result over budget in stream", tc["name"])
                    result_json = _truncate_tool_result(result_json)
                tool_result_texts.append(result_json)
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result_json,
                })

            messages = messages + [assistant_message] + tool_results

    yield sse({"type": "error", "message": f"Agent exceeded {MAX_ITERATIONS} iterations"})
