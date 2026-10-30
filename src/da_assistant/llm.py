"""Gemini access through the google-genai SDK."""

import time
from dataclasses import dataclass

from google import genai
from google.genai import types

from da_assistant.config import ConfigError, Settings


@dataclass(frozen=True)
class LLMReply:
    text: str
    model: str
    input_tokens: int
    # Includes thinking tokens: they are billed as output.
    output_tokens: int
    latency_s: float


class Gemini:
    def __init__(self, client: genai.Client, *, model: str, fallback_model: str) -> None:
        self.client = client
        self.model = model
        self.fallback_model = fallback_model

    @classmethod
    def from_settings(cls, settings: Settings) -> "Gemini":
        if settings.gemini_api_key is None:
            raise ConfigError(
                "GEMINI_API_KEY is not set. Get a free key at https://aistudio.google.com/apikey "
                "and put it in the .env file."
            )
        client = genai.Client(
            api_key=settings.gemini_api_key.get_secret_value(),
            http_options=types.HttpOptions(timeout=int(settings.llm_timeout_s * 1000)),
        )
        return cls(client, model=settings.llm_model, fallback_model=settings.llm_fallback_model)

    def generate(
        self, prompt: str, *, system: str | None = None, model: str | None = None
    ) -> LLMReply:
        model = model or self.model
        start = time.perf_counter()
        response = self.client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(system_instruction=system),
        )
        usage = response.usage_metadata or types.GenerateContentResponseUsageMetadata()
        return LLMReply(
            text=response.text or "",
            model=model,
            input_tokens=usage.prompt_token_count or 0,
            output_tokens=(usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0),
            latency_s=time.perf_counter() - start,
        )
