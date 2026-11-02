import pytest
from google.api_core import exceptions as gcp_errors
from google.auth import exceptions as auth_errors
from google.genai import errors as genai_errors

from da_assistant.config import ConfigError
from da_assistant.errors import explain, is_rate_limit


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
