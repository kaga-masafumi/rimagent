import os

import pytest

from rimagent.providers import OpenAIProvider


@pytest.fixture
def env_root(tmp_path, monkeypatch):
    root = tmp_path / "日本語パス"
    root.mkdir()
    monkeypatch.setattr("rimagent.providers.ROOT", root)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return root


def test_root_dotenv_is_read_independent_of_cwd_without_mutating_environment(env_root, monkeypatch, tmp_path):
    (env_root / ".env").write_text('OPENAI_API_KEY="test-key" # comment\n', encoding="utf-8-sig")
    monkeypatch.chdir(tmp_path)
    assert OpenAIProvider({}).api_key == "test-key"
    assert "OPENAI_API_KEY" not in os.environ


def test_existing_environment_wins_over_dotenv(env_root, monkeypatch):
    (env_root / ".env").write_text('OPENAI_API_KEY=file-key\n', encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "environment-key")
    assert OpenAIProvider({}).api_key == "environment-key"


def test_custom_key_name_and_literal_dollar_values(env_root, monkeypatch):
    monkeypatch.delenv("RIMAGENT_TEST_KEY", raising=False)
    (env_root / ".env").write_text("RIMAGENT_TEST_KEY='literal-${SECRET}'\n", encoding="utf-8")
    assert OpenAIProvider({"api_key_env": "RIMAGENT_TEST_KEY"}).api_key == "literal-${SECRET}"


@pytest.mark.parametrize("contents", [None, "OPENAI_API_KEY=\n", "OTHER_KEY=ignored\n"])
def test_missing_or_empty_key_has_safe_error(env_root, contents):
    if contents is not None:
        (env_root / ".env").write_text(contents, encoding="utf-8")
    with pytest.raises(ValueError, match=r"OPENAI_API_KEY.*\.env"):
        OpenAIProvider({})
