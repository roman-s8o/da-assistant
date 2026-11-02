from datetime import UTC, datetime

import pytest
from fakes import quota_error
from google.api_core import exceptions as gcp_errors
from google.auth import exceptions as auth_errors
from google.genai import errors as genai_errors

from da_assistant import errors
from da_assistant.config import ConfigError
from da_assistant.errors import (
    daily_quota_reached,
    daily_quota_resets_at,
    explain,
    is_rate_limit,
)


def genai_error(code: int, message: str, status: str = "") -> genai_errors.APIError:
    payload = {"error": {"code": code, "message": message, "status": status}}
    cls = genai_errors.ServerError if code >= 500 else genai_errors.ClientError
    return cls(code, payload)


@pytest.mark.parametrize(
    ("exc", "hint"),
    [
        (auth_errors.RefreshError("Reauthentication is needed."), "gcloud auth"),
        (gcp_errors.Forbidden("Access Denied"), "BigQuery API is enabled"),
        (gcp_errors.BadRequest("The project p has not enabled BigQuery."), "DA_GCP_PROJECT"),
        (genai_error(400, "API key not valid."), "GEMINI_API_KEY"),
        (genai_error(404, "models/x is not found"), "DA_LLM_MODEL"),
        (genai_error(429, "Resource exhausted"), "rate limit"),
        (genai_error(503, "The model is busy."), "Try again later"),
    ],
)
def test_explain_adds_a_hint(exc, hint):
    assert hint in explain(exc)


def test_explain_keeps_only_the_first_line():
    assert explain(RuntimeError("first line\nlong details")) == "first line"


def test_explain_drops_the_request_url():
    exc = gcp_errors.BadRequest("POST https://bigquery.googleapis.com/x: Bad query")

    assert explain(exc) == "400 Bad query"


def test_explain_gemini_error_without_raw_json():
    exc = genai_error(503, "The model is busy.", "UNAVAILABLE")

    assert explain(exc).startswith("503 UNAVAILABLE: The model is busy.\nHint:")


def test_explain_finds_the_api_error_inside_a_wrapper():
    # LangChain raises its own error class with the SDK error as the cause.
    try:
        try:
            raise genai_error(404, "models/x is not found", "NOT_FOUND")
        except genai_errors.APIError as inner:
            raise RuntimeError("Error calling model 'x'") from inner
    except RuntimeError as wrapper:
        text = explain(wrapper)

    assert text.startswith("404 NOT_FOUND: models/x is not found")
    assert "DA_LLM_MODEL" in text


def test_explain_config_error_is_shown_as_is():
    assert explain(ConfigError("line 1\nline 2")) == "line 1\nline 2"


def test_is_rate_limit():
    wrapped = RuntimeError("LangChain wrapper")
    wrapped.__cause__ = genai_error(429, "Resource exhausted")

    assert is_rate_limit(genai_error(429, "Resource exhausted"))
    assert is_rate_limit(wrapped)
    assert is_rate_limit(gcp_errors.TooManyRequests("Quota exceeded"))
    assert not is_rate_limit(genai_error(503, "The model is busy."))
    assert not is_rate_limit(RuntimeError("other"))


def test_daily_quota_reached():
    wrapped = RuntimeError("LangChain wrapper")
    wrapped.__cause__ = quota_error("Day")

    assert daily_quota_reached(quota_error("Day"))
    assert daily_quota_reached(wrapped)
    assert not daily_quota_reached(quota_error("Minute"))
    assert not daily_quota_reached(genai_error(429, "Resource exhausted"))
    assert not daily_quota_reached(genai_error(503, "The model is busy."))


@pytest.mark.parametrize(
    ("now", "reset"),
    [
        # Winter: midnight Pacific is 08:00 UTC.
        (datetime(2026, 11, 2, 12, 16, tzinfo=UTC), datetime(2026, 11, 3, 8, 0, tzinfo=UTC)),
        (datetime(2026, 11, 3, 7, 59, tzinfo=UTC), datetime(2026, 11, 3, 8, 0, tzinfo=UTC)),
        # Summer: 07:00 UTC.
        (datetime(2026, 7, 1, 12, 0, tzinfo=UTC), datetime(2026, 7, 2, 7, 0, tzinfo=UTC)),
    ],
)
def test_daily_quota_resets_at_midnight_pacific(now, reset):
    assert daily_quota_resets_at(now) == reset


def test_reset_time_without_time_zone_data(monkeypatch):
    def no_zone(name):
        raise errors.ZoneInfoNotFoundError(name)

    monkeypatch.setattr(errors, "ZoneInfo", no_zone)
    now = datetime(2026, 11, 2, 12, 16, tzinfo=UTC)

    assert errors.daily_quota_resets_at(now) == datetime(2026, 11, 3, 8, 0, tzinfo=UTC)


def test_a_daily_quota_gets_its_own_hint():
    assert "daily quota" in explain(quota_error("Day"))
    assert "Wait a minute" in explain(quota_error("Minute"))
