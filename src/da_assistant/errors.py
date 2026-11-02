"""Short, readable error messages, with a hint when we know the fix."""

import re
from datetime import UTC, datetime, time, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from google.api_core import exceptions as gcp_errors
from google.auth import exceptions as auth_errors
from google.genai import errors as genai_errors

from da_assistant.config import ConfigError

# Google API errors start with the HTTP method and URL, which only add noise.
_REQUEST_PREFIX = re.compile(r"(GET|POST|PUT|PATCH|DELETE) \S+: ")

_API_ERRORS = (genai_errors.APIError, gcp_errors.GoogleAPICallError, auth_errors.GoogleAuthError)


def explain(exc: BaseException) -> str:
    """One line about what went wrong, plus a hint on how to fix it when we know one."""
    if isinstance(exc, ConfigError):
        return str(exc)
    exc = _api_cause(exc)
    message = short_message(exc)
    hint = _hint(exc)
    return f"{message}\nHint: {hint}" if hint else message


def is_rate_limit(exc: BaseException) -> bool:
    """The API refused the call because of a rate limit or a quota (HTTP 429)."""
    exc = _api_cause(exc)
    if isinstance(exc, genai_errors.APIError):
        return exc.code == 429
    return isinstance(exc, gcp_errors.TooManyRequests)


def daily_quota_reached(exc: BaseException) -> bool:
    """A quota per day is used up, for example the 500 free requests a day of a model.

    Unlike a limit per minute, waiting a little or retrying does not help: the quota comes
    back only when it resets. Another model has a quota of its own.
    """
    if not is_rate_limit(exc):
        return False
    return any(
        "PerDay" in str(violation.get("quotaId", ""))
        for detail in _error_details(exc)
        for violation in detail.get("violations") or ()
        if isinstance(violation, dict)
    )


def daily_quota_resets_at(now: datetime | None = None) -> datetime:
    """When the Gemini quotas per day reset next, in UTC.

    They reset at midnight Pacific time (https://ai.google.dev/gemini-api/docs/rate-limits).
    """
    try:
        zone: tzinfo = ZoneInfo("America/Los_Angeles")
    except ZoneInfoNotFoundError:  # Windows without the tzdata package: standard time
        zone = timezone(timedelta(hours=-8))
    pacific = (now or datetime.now(UTC)).astimezone(zone)
    tomorrow = pacific.date() + timedelta(days=1)
    return datetime.combine(tomorrow, time(0), tzinfo=zone).astimezone(UTC)


def _error_details(exc: BaseException) -> list[dict[str, Any]]:
    """The structured details of a Gemini API error, such as the quota that was hit."""
    details = getattr(_api_cause(exc), "details", None)
    if isinstance(details, dict):
        details = (details.get("error") or {}).get("details")
    if not isinstance(details, list):
        return []
    return [detail for detail in details if isinstance(detail, dict)]


def short_message(exc: BaseException, max_chars: int = 300) -> str:
    if isinstance(exc, genai_errors.APIError) and exc.message:
        text = f"{exc.code} {exc.status}: {exc.message}"
    else:
        text = _REQUEST_PREFIX.sub("", str(exc))
    lines = text.strip().splitlines()
    return lines[0][:max_chars] if lines else type(exc).__name__


def _api_cause(exc: BaseException) -> BaseException:
    """LangChain wraps the API errors of the SDK. Use the original error when there is one."""
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, _API_ERRORS):
            return current
        current = current.__cause__
    return exc


def _hint(exc: BaseException) -> str | None:
    if isinstance(exc, auth_errors.GoogleAuthError):
        return "run `gcloud auth application-default login`."
    text = str(exc)
    if "has not enabled BigQuery" in text or "BigQuery API has not been used" in text:
        return (
            "enable the BigQuery API in this project, "
            "or set DA_GCP_PROJECT to a project where it is enabled."
        )
    if isinstance(exc, gcp_errors.Forbidden):
        return (
            "check that the BigQuery API is enabled in the billing project "
            "and that your account can run jobs there."
        )
    if isinstance(exc, genai_errors.APIError):
        if exc.code in (401, 403) or (exc.code == 400 and "API key" in text):
            return "check GEMINI_API_KEY."
        if exc.code == 404:
            return "model not found. Set DA_LLM_MODEL or DA_LLM_FALLBACK_MODEL."
        if daily_quota_reached(exc):
            reset = daily_quota_resets_at()
            return (
                "the daily quota of this Gemini model is used up. It resets at midnight "
                f"Pacific time ({reset:%H:%M} UTC)."
            )
        if exc.code == 429:
            return "rate limit or quota reached. Wait a minute and try again."
        if exc.code >= 500:
            return "Gemini service error. Try again later."
    return None
