import pytest

from da_assistant.config import ConfigError, load_settings
from da_assistant.llm import chat_model, resilient_model


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    return load_settings()


def test_models_have_no_retries_of_their_own(settings):
    # ResilientChat does the retries, so each attempt shows in the traces.
    assert chat_model(settings).max_retries == 1


def test_timeout_can_be_shorter_for_the_guard(settings):
    assert chat_model(settings).timeout == settings.llm_timeout_s
    assert chat_model(settings, timeout_s=10).timeout == 10


def test_fallback_only_when_it_is_another_model(settings):
    assert resilient_model(settings, "a", "b").fallback is not None
    assert resilient_model(settings, "a", "a").fallback is None
    assert resilient_model(settings, "a", None).fallback is None


def test_a_missing_key_is_a_clear_error():
    with pytest.raises(ConfigError, match="GEMINI_API_KEY is not set"):
        chat_model(load_settings())
