"""
OpenAI tool-calling loop.

Budget: ~2500 tokens total per turn (system 200 + user 100 + tool results 1800 + response 400).
The loop logs actual usage from the API response so measure_tokens.py can track it.
"""
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import AsyncGenerator

from openai import AsyncOpenAI, AuthenticationError

from src.agent.prompts import SYSTEM_PROMPT
from src.db.client import get_store
from src.tools.definitions import select_tools
from src.tools.dispatcher import dispatch_tool
from src.tools.resolve_airports import resolve_origin_destination

log = logging.getLogger(__name__)

_DEFAULT_MODEL = "gpt-4o-mini"
MAX_RESPONSE_TOKENS = 400
MAX_ITERATIONS = 10
WARN_TOOL_TOKENS = 1800  # tool-result budget for the WHOLE turn, not per call — see
# how tool_tokens_used is threaded through run_agent/stream_agent below

# Numbers with 2+ digits — catches prices/scores but not stray single digits ("1 stop").
_NUMBER_RE = re.compile(r"\d{2,}(?:[.,]\d+)?")


async def validate_openai_key(api_key: str) -> bool:
    """One minimal, cheap request instead of waiting for the first real chat
    turn to fail — lets a bad key fail once, at startup, with one clear
    message (mirrors validate_token in src/scraper/travelpayouts.py). Returns
    False only on a confirmed 401 (invalid/expired/revoked key); any other
    outcome (including network errors) is treated as "can't tell, don't
    block" and returns True."""
    client = AsyncOpenAI(api_key=api_key)
    try:
        await client.models.list()
    except AuthenticationError:
        return False
    except Exception:
        pass
    return True


@dataclass
class AgentResult:
    response: str
    input_tokens: int
    output_tokens: int
    tool_calls: int = 0
    iterations: int = 0
    tool_token_warnings: list[str] = field(default_factory=list)
    grounding_warnings: list[str] = field(default_factory=list)
    openai_egress_bytes: int | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def _read_tx_bytes() -> int | None:
    """Actual bytes transmitted on this process's network interface, read from the
    kernel counter -- not derived from token counts. This is the only place data
    leaves the cluster during a turn (the OpenAI call), so a before/after delta
    around it measures egress directly instead of assuming it from `usage.prompt_tokens`.
    Returns None where the counter isn't available (non-Linux dev machines) so callers
    degrade to reporting "not measured" rather than a fabricated number."""
    path = os.getenv("EGRESS_IFACE_STATS_PATH", "/sys/class/net/eth0/statistics/tx_bytes")
    try:
        with open(path) as f:
            return int(f.read().strip())
    except OSError:
        return None


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


async def _try_fast_path(
    user_message: str, preferences: dict | None
) -> tuple[dict, dict] | None:
    """Single-call fast path: resolve an unambiguous "from X to Y" message via
    RavenDB (src/tools/resolve_airports.py) and pre-fetch search_routes
    ourselves, so the model never has to spend a tool-calling round trip
    deciding to call it. Returns (resolved, tool_result) on success; None
    (ambiguous message, no match, or any failure) means the caller falls back
    to the normal multi-turn tool-calling loop below, unchanged."""
    try:
        resolved = resolve_origin_destination(get_store(), user_message)
        if not resolved:
            return None
        tool_result = await dispatch_tool("search_routes", resolved, preferences=preferences)
        return resolved, tool_result
    except Exception:
        log.exception("Fast-path resolution/search failed -- falling back to tool-calling flow")
        return None


def _build_fast_path_content(user_message: str, resolved: dict, tool_result: dict) -> str:
    result_json = json.dumps(tool_result, default=str)
    return (
        f"{user_message}\n\n"
        f"[search_routes({resolved['origin']}->{resolved['destination']}) already run, "
        f"result: {result_json}]"
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

    fast_path = await _try_fast_path(user_message, preferences)
    if fast_path is not None:
        resolved, tool_result = fast_path
        user_content = _build_fast_path_content(user_message, resolved, tool_result)
        messages = (
            [{"role": "system", "content": system_content}]
            + prior_turns
            + [{"role": "user", "content": user_content}]
        )
        tx_before = _read_tx_bytes()
        response = await client.chat.completions.create(
            model=os.getenv("LLM_MODEL", _DEFAULT_MODEL),
            max_tokens=MAX_RESPONSE_TOKENS,
            messages=messages,
        )
        tx_after = _read_tx_bytes()
        egress_bytes = tx_after - tx_before if tx_before is not None and tx_after is not None else None
        text = response.choices[0].message.content
        if text is not None:
            known_data = system_content + user_content
            ungrounded = _find_ungrounded_numbers(text, known_data)
            if ungrounded:
                warning = f"reply contains numbers not seen in tool output or preferences: {ungrounded}"
                log.warning(warning)
            return AgentResult(
                response=text,
                input_tokens=response.usage.prompt_tokens,
                output_tokens=response.usage.completion_tokens,
                tool_calls=1,
                iterations=1,
                grounding_warnings=[warning] if ungrounded else [],
                openai_egress_bytes=egress_bytes,
            )
        # No text content (unexpected without tools offered) -- fall through to
        # the normal loop below rather than return something broken.

    tools = select_tools(user_message)
    messages = (
        [{"role": "system", "content": system_content}]
        + prior_turns
        + [{"role": "user", "content": user_message}]
    )

    total_input = 0
    total_output = 0
    tool_calls = 0
    tool_tokens_used = 0  # cumulative across the WHOLE turn — WARN_TOOL_TOKENS is
    # a per-turn budget, not a per-call one; see _truncate_tool_result call below
    token_warnings: list[str] = []
    grounding_warnings: list[str] = []
    tool_result_texts: list[str] = []
    total_egress_bytes: int | None = 0

    for iteration in range(1, MAX_ITERATIONS + 1):
        # Force at least one tool call on the first turn — otherwise "auto" lets the
        # model skip search_routes/get_live_prices entirely and answer from parametric
        # knowledge, which is the main way ungrounded city names/prices sneak in.
        tool_choice = "required" if iteration == 1 else "auto"
        tx_before = _read_tx_bytes()
        response = await client.chat.completions.create(
            model=os.getenv("LLM_MODEL", _DEFAULT_MODEL),
            max_tokens=MAX_RESPONSE_TOKENS,
            tools=tools,
            tool_choice=tool_choice,
            messages=messages,
        )
        tx_after = _read_tx_bytes()
        if total_egress_bytes is not None and tx_before is not None and tx_after is not None:
            total_egress_bytes += tx_after - tx_before
        else:
            total_egress_bytes = None

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
                openai_egress_bytes=total_egress_bytes,
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
                remaining_budget = max(0, WARN_TOOL_TOKENS - tool_tokens_used)
                if estimated > remaining_budget:
                    warning = (
                        f"{tc.function.name} result ~{estimated} tokens, only {remaining_budget} "
                        f"left of this turn's {WARN_TOOL_TOKENS}-token tool budget"
                    )
                    log.warning(warning)
                    token_warnings.append(warning)
                    result_json = _truncate_tool_result(result_json, max_tokens=remaining_budget)
                    estimated = _estimate_tokens(result_json)
                tool_tokens_used += estimated

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

    def sse(data: dict) -> str:
        return f"data: {json.dumps(data)}\n\n"

    # Same single-call fast path as run_agent -- see _try_fast_path's docstring.
    fast_path = await _try_fast_path(user_message, preferences)
    if fast_path is not None:
        resolved, tool_result = fast_path
        yield sse({"type": "tool_call", "name": "search_routes"})
        user_content = _build_fast_path_content(user_message, resolved, tool_result)
        fp_messages = (
            [{"role": "system", "content": system_content}]
            + prior_turns
            + [{"role": "user", "content": user_content}]
        )
        tx_before = _read_tx_bytes()
        fp_stream = await client.chat.completions.create(
            model=os.getenv("LLM_MODEL", _DEFAULT_MODEL),
            max_tokens=MAX_RESPONSE_TOKENS,
            messages=fp_messages,
            stream=True,
            stream_options={"include_usage": True},
        )
        fp_content: list[str] = []
        fp_input, fp_output = 0, 0
        async for chunk in fp_stream:
            if chunk.usage:
                fp_input += chunk.usage.prompt_tokens
                fp_output += chunk.usage.completion_tokens
                continue
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                fp_content.append(delta.content)
                yield sse({"type": "delta", "text": delta.content})
        # Measured after the stream is fully drained, not right after create() --
        # create(stream=True) returns before any chunk has actually crossed the wire.
        tx_after = _read_tx_bytes()
        egress_bytes = tx_after - tx_before if tx_before is not None and tx_after is not None else None

        final_text = "".join(fp_content)
        if final_text:
            known_data = system_content + user_content
            ungrounded = _find_ungrounded_numbers(final_text, known_data)
            if ungrounded:
                log.warning(
                    "reply contains numbers not seen in tool output or preferences: %s",
                    ungrounded,
                )
            yield sse({
                "type": "done",
                "response": final_text,
                "input_tokens": fp_input,
                "output_tokens": fp_output,
                "total_tokens": fp_input + fp_output,
                "tool_calls": 1,
                "grounding_warnings": ungrounded,
                "openai_egress_bytes": egress_bytes,
            })
            return
        # No content streamed (unexpected) -- fall through to the normal loop below.

    tools = select_tools(user_message)
    messages = (
        [{"role": "system", "content": system_content}]
        + prior_turns
        + [{"role": "user", "content": user_message}]
    )
    total_input, total_output, tool_calls_count = 0, 0, 0
    tool_tokens_used = 0  # cumulative across the WHOLE turn — see run_agent
    tool_result_texts: list[str] = []
    total_egress_bytes: int | None = 0

    for iteration in range(1, MAX_ITERATIONS + 1):
        accumulated_content: list[str] = []
        accumulated_tool_calls: dict[int, dict] = {}
        finish_reason = None

        # Same first-turn grounding guard as run_agent — see comment there.
        tool_choice = "required" if iteration == 1 else "auto"
        tx_before = _read_tx_bytes()
        stream = await client.chat.completions.create(
            model=os.getenv("LLM_MODEL", _DEFAULT_MODEL),
            max_tokens=MAX_RESPONSE_TOKENS,
            messages=messages,
            tools=tools,
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

        # Measured after the stream is fully drained -- see the fast-path comment above.
        tx_after = _read_tx_bytes()
        if total_egress_bytes is not None and tx_before is not None and tx_after is not None:
            total_egress_bytes += tx_after - tx_before
        else:
            total_egress_bytes = None

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
                "response": final_text,
                "input_tokens": total_input,
                "output_tokens": total_output,
                "total_tokens": total_input + total_output,
                "tool_calls": tool_calls_count,
                "grounding_warnings": ungrounded,
                "openai_egress_bytes": total_egress_bytes,
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

                estimated = _estimate_tokens(result_json)
                remaining_budget = max(0, WARN_TOOL_TOKENS - tool_tokens_used)
                if estimated > remaining_budget:
                    log.warning(
                        "%s result ~%d tokens, only %d left of this turn's %d-token tool budget",
                        tc["name"], estimated, remaining_budget, WARN_TOOL_TOKENS,
                    )
                    result_json = _truncate_tool_result(result_json, max_tokens=remaining_budget)
                    estimated = _estimate_tokens(result_json)
                tool_tokens_used += estimated

                tool_result_texts.append(result_json)
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result_json,
                })

            messages = messages + [assistant_message] + tool_results

    yield sse({"type": "error", "message": f"Agent exceeded {MAX_ITERATIONS} iterations"})
