"""
In-process flag for whether TRAVELPAYOUTS_TOKEN passed the lightweight startup
check in src/agent/app.py — lets get_live_prices fail fast with one clear
message instead of repeating the same 401 from Travelpayouts on every
cache-miss tool call.
"""

_valid: bool | None = None
"""None = never checked (TRAVELPAYOUTS_TOKEN unset)."""


def set_valid(value: bool | None) -> None:
    global _valid
    _valid = value


def is_valid() -> bool | None:
    return _valid
