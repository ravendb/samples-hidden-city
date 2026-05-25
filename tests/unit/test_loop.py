"""
Unit tests for the agent loop — OpenAI client is fully mocked.
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agent.loop import AgentResult, run_agent


def _make_end_turn_response(text: str, input_tokens: int = 100, output_tokens: int = 50):
    response = MagicMock()
    choice = MagicMock()
    choice.finish_reason = "stop"
    choice.message.content = text
    choice.message.tool_calls = None
    response.choices = [choice]
    response.usage.prompt_tokens = input_tokens
    response.usage.completion_tokens = output_tokens
    return response


def _make_tool_use_response(tool_name: str, tool_input: dict, tool_id: str = "call_01"):
    response = MagicMock()
    choice = MagicMock()
    choice.finish_reason = "tool_calls"
    choice.message.content = None
    tc = MagicMock()
    tc.id = tool_id
    tc.function.name = tool_name
    tc.function.arguments = json.dumps(tool_input)
    choice.message.tool_calls = [tc]
    response.choices = [choice]
    response.usage.prompt_tokens = 80
    response.usage.completion_tokens = 30
    return response


class TestRunAgent:
    @pytest.mark.asyncio
    async def test_end_turn_no_tools(self):
        mock_response = _make_end_turn_response("Here are your flights.")

        with patch("src.agent.loop.AsyncOpenAI") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value = mock_client
            mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

            result = await run_agent(user_message="Find WAW→LHR", prior_turns=[])

        assert result.response == "Here are your flights."
        assert result.tool_calls == 0
        assert result.iterations == 1
        assert result.total_tokens == 150

    @pytest.mark.asyncio
    async def test_one_tool_call_then_end(self):
        tool_response = _make_tool_use_response(
            "search_routes", {"origin": "WAW", "destination": "LHR"}
        )
        end_response = _make_end_turn_response("Found 2 routes.")

        dispatched = []

        async def mock_dispatch(name, tool_input):
            dispatched.append(name)
            return {"routes": [], "count": 0}

        with (
            patch("src.agent.loop.AsyncOpenAI") as mock_client_cls,
            patch("src.agent.loop.dispatch_tool", side_effect=mock_dispatch),
        ):
            mock_client = AsyncMock()
            mock_client_cls.return_value = mock_client
            mock_client.chat.completions.create = AsyncMock(
                side_effect=[tool_response, end_response]
            )
            result = await run_agent(user_message="Find WAW→LHR", prior_turns=[])

        assert result.tool_calls == 1
        assert dispatched == ["search_routes"]
        assert result.response == "Found 2 routes."
        assert result.iterations == 2

    @pytest.mark.asyncio
    async def test_prior_turns_included_in_messages(self):
        end_response = _make_end_turn_response("Here you go.")
        captured_messages = []

        async def capture_create(**kwargs):
            captured_messages.extend(kwargs["messages"])
            return end_response

        with patch("src.agent.loop.AsyncOpenAI") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value = mock_client
            mock_client.chat.completions.create = capture_create

            await run_agent(
                user_message="New question",
                prior_turns=[
                    {"role": "user", "content": "Old question"},
                    {"role": "assistant", "content": "Old answer"},
                ],
            )

        # messages = [system, prior_user, prior_assistant, new_user]
        assert captured_messages[1]["content"] == "Old question"
        assert captured_messages[2]["content"] == "Old answer"
        assert captured_messages[3]["content"] == "New question"

    @pytest.mark.asyncio
    async def test_token_warning_on_large_tool_result(self):
        tool_response = _make_tool_use_response("search_routes", {"origin": "WAW"})
        end_response = _make_end_turn_response("Done.")

        large_result = {"routes": [{"data": "x" * 4000}]}  # ~1000 tokens

        with (
            patch("src.agent.loop.AsyncOpenAI") as mock_client_cls,
            patch(
                "src.agent.loop.dispatch_tool",
                new=AsyncMock(return_value=large_result),
            ),
        ):
            mock_client = AsyncMock()
            mock_client_cls.return_value = mock_client
            mock_client.chat.completions.create = AsyncMock(
                side_effect=[tool_response, end_response]
            )
            result = await run_agent(user_message="Search routes", prior_turns=[])

        assert len(result.tool_token_warnings) > 0

    @pytest.mark.asyncio
    async def test_max_iterations_raises(self):
        always_tool = _make_tool_use_response("search_routes", {"origin": "WAW"})

        with (
            patch("src.agent.loop.AsyncOpenAI") as mock_client_cls,
            patch(
                "src.agent.loop.dispatch_tool",
                new=AsyncMock(return_value={"routes": []}),
            ),
            patch("src.agent.loop.MAX_ITERATIONS", 3),
        ):
            mock_client = AsyncMock()
            mock_client_cls.return_value = mock_client
            mock_client.chat.completions.create = AsyncMock(return_value=always_tool)

            with pytest.raises(RuntimeError, match="exceeded"):
                await run_agent(user_message="Loop forever", prior_turns=[])
