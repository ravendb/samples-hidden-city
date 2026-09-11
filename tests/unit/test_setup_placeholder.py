"""
Regression tests for the .env.example placeholder bug: a fresh .env copied
from .env.example carries literal placeholder values (OPENAI_API_KEY=sk-...,
TRAVELPAYOUTS_TOKEN=...) that must never be reported or reused as real keys.
"""
import os

import pytest

from src.agent import app as app_module


async def _fake_validate_openai_key(api_key: str) -> bool:
    return True


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("OPENAI_API_KEY", "RAVENDB_LICENSE", "TRAVELPAYOUTS_TOKEN"):
        monkeypatch.delenv(key, raising=False)


class TestIsKeyConfigured:
    def test_placeholder_openai_key_is_not_configured(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-...")
        assert app_module._is_key_configured("OPENAI_API_KEY") is False

    def test_placeholder_travelpayouts_token_is_not_configured(self, monkeypatch):
        monkeypatch.setenv("TRAVELPAYOUTS_TOKEN", "...")
        assert app_module._is_key_configured("TRAVELPAYOUTS_TOKEN") is False

    def test_real_openai_key_is_configured(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-real1234567890")
        assert app_module._is_key_configured("OPENAI_API_KEY") is True

    def test_missing_key_is_not_configured(self):
        assert app_module._is_key_configured("OPENAI_API_KEY") is False


class TestSetupStatusEndpoint:
    async def test_placeholder_key_reported_as_not_set(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-...")
        status = await app_module.api_setup_status()
        assert status["openai_api_key_set"] is False

    async def test_real_key_reported_as_set(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-real1234567890")
        status = await app_module.api_setup_status()
        assert status["openai_api_key_set"] is True

    async def test_placeholder_travelpayouts_token_reported_as_not_set(self, monkeypatch):
        monkeypatch.setenv("TRAVELPAYOUTS_TOKEN", "...")
        status = await app_module.api_setup_status()
        assert status["travelpayouts_set"] is False


class TestSetEnvValue:
    def test_updates_existing_key_without_touching_other_lines(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "# OpenAI\nOPENAI_API_KEY=sk-...\n\n# Flight APIs\nTRAVELPAYOUTS_TOKEN=...\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(app_module, "_ENV_FILE", env_file)

        app_module._set_env_value("OPENAI_API_KEY", "sk-proj-real1234567890")

        content = env_file.read_text(encoding="utf-8")
        assert "OPENAI_API_KEY=sk-proj-real1234567890" in content
        assert "# OpenAI" in content
        assert "TRAVELPAYOUTS_TOKEN=..." in content  # untouched, not clobbered

    def test_appends_key_missing_from_file(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("LLM_MODEL=gpt-4o-mini\n", encoding="utf-8")
        monkeypatch.setattr(app_module, "_ENV_FILE", env_file)

        app_module._set_env_value("OPENAI_API_KEY", "sk-proj-real1234567890")

        lines = env_file.read_text(encoding="utf-8").splitlines()
        assert "OPENAI_API_KEY=sk-proj-real1234567890" in lines
        assert "LLM_MODEL=gpt-4o-mini" in lines

    def test_creates_file_if_missing(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        monkeypatch.setattr(app_module, "_ENV_FILE", env_file)

        app_module._set_env_value("OPENAI_API_KEY", "sk-proj-real1234567890")

        assert env_file.read_text(encoding="utf-8") == "OPENAI_API_KEY=sk-proj-real1234567890\n"


class TestApiSetupEndpoint:
    async def test_submitted_key_overwrites_placeholder_on_disk_and_in_env(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("OPENAI_API_KEY=sk-...\n", encoding="utf-8")
        monkeypatch.setattr(app_module, "_ENV_FILE", env_file)
        monkeypatch.setattr("src.agent.loop.validate_openai_key", _fake_validate_openai_key)

        result = await app_module.api_setup(
            app_module.SetupRequest(openai_api_key="sk-proj-real1234567890")
        )

        assert result == {"status": "ok"}
        assert os.environ["OPENAI_API_KEY"] == "sk-proj-real1234567890"
        assert "OPENAI_API_KEY=sk-proj-real1234567890" in env_file.read_text(encoding="utf-8")
