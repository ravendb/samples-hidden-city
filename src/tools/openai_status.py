"""
In-process flag for whether OPENAI_API_KEY passed the lightweight startup
check in src/agent/app.py — lets the chat endpoint fail fast with one clear
message instead of a raw 401 from OpenAI on the first real chat call.
"""

_valid: bool | None = None
"""None = never checked (OPENAI_API_KEY unset)."""


def set_valid(value: bool | None) -> None:
    global _valid
    _valid = value


def is_valid() -> bool | None:
    return _valid
