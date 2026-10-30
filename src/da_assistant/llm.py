"""Gemini chat models through LangChain (langchain-google-genai)."""

from langchain_google_genai import ChatGoogleGenerativeAI

from da_assistant.config import ConfigError, Settings
from da_assistant.resilience import ResilientChat


def chat_model(
    settings: Settings, model: str | None = None, *, timeout_s: float | None = None
) -> ChatGoogleGenerativeAI:
    """One Gemini model, with no retries of its own. The arguments override the settings."""
    if settings.gemini_api_key is None:
        raise ConfigError(
            "GEMINI_API_KEY is not set. Get a free key at https://aistudio.google.com/apikey "
            "and put it in the .env file."
        )
    return ChatGoogleGenerativeAI(
        model=model or settings.llm_model,
        google_api_key=settings.gemini_api_key.get_secret_value(),
        timeout=timeout_s or settings.llm_timeout_s,
        # One attempt: ResilientChat does the retries, so each one shows in the traces.
        max_retries=1,
        # None keeps the default temperature of the model.
        temperature=None,
    )


def resilient_model(
    settings: Settings, primary: str, fallback: str | None, *, timeout_s: float | None = None
) -> ResilientChat:
    """A model with retries, and a fallback model when it is a different one."""
    if fallback == primary:
        fallback = None
    return ResilientChat(
        chat_model(settings, primary, timeout_s=timeout_s),
        chat_model(settings, fallback, timeout_s=timeout_s) if fallback else None,
        primary_name=primary,
        fallback_name=fallback,
        attempts=settings.llm_attempts,
        retry_window_s=settings.llm_retry_window_s,
        cooldown_s=settings.llm_cooldown_s,
    )
