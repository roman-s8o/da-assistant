"""Gemini chat models through LangChain (langchain-google-genai)."""

from langchain_google_genai import ChatGoogleGenerativeAI

from da_assistant.config import ConfigError, Settings


def chat_model(settings: Settings, model: str | None = None) -> ChatGoogleGenerativeAI:
    """The chat model for the agent. `model` overrides the model from the settings."""
    if settings.gemini_api_key is None:
        raise ConfigError(
            "GEMINI_API_KEY is not set. Get a free key at https://aistudio.google.com/apikey "
            "and put it in the .env file."
        )
    return ChatGoogleGenerativeAI(
        model=model or settings.llm_model,
        google_api_key=settings.gemini_api_key.get_secret_value(),
        timeout=settings.llm_timeout_s,
        # The total number of attempts. The google-genai SDK retries
        # rate limit (429) and server (5xx) errors with exponential backoff.
        max_retries=settings.llm_attempts,
        # None keeps the default temperature of the model.
        temperature=None,
    )
