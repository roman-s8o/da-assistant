from types import SimpleNamespace

import pandas as pd
import pytest
from google.api_core import exceptions as gcp_errors
from google.auth import exceptions as auth_errors
from google.genai import errors as genai_errors

from da_assistant import health
from da_assistant.config import ConfigError, load_settings
from da_assistant.llm import LLMReply


def genai_error(code: int, message: str) -> genai_errors.APIError:
    return genai_errors.ClientError(code, {"error": {"code": code, "message": message}})


@pytest.mark.parametrize(
    ("exc", "hint"),
    [
        (auth_errors.RefreshError("Reauthentication is needed."), "gcloud auth"),
        (gcp_errors.Forbidden("Access Denied"), "BigQuery API is enabled"),
        (gcp_errors.BadRequest("The project p has not enabled BigQuery."), "DA_GCP_PROJECT"),
        (genai_error(400, "API key not valid."), "GEMINI_API_KEY"),
        (genai_error(404, "models/x is not found"), "DA_LLM_MODEL"),
        (genai_error(429, "Resource exhausted"), "rate limit"),
    ],
)
def test_explain_adds_a_hint(exc, hint):
    assert hint in health.explain(exc)


def test_explain_keeps_only_the_first_line():
    assert health.explain(RuntimeError("first line\nlong details")) == "first line"


def test_explain_drops_the_request_url():
    exc = gcp_errors.BadRequest("POST https://bigquery.googleapis.com/x: Bad query")

    assert health.explain(exc) == "400 Bad query"


def test_explain_gemini_error_without_raw_json():
    exc = genai_errors.ServerError(
        503, {"error": {"code": 503, "message": "The model is busy.", "status": "UNAVAILABLE"}}
    )

    assert health.explain(exc).startswith("503 UNAVAILABLE: The model is busy.\nHint:")


def test_explain_config_error_is_shown_as_is():
    assert health.explain(ConfigError("line 1\nline 2")) == "line 1\nline 2"


def test_row_count_sql_covers_all_tables():
    sql = health.row_count_sql("p.d")

    for table in health.TABLES:
        assert f"`p.d.{table}`" in sql


class FakeWarehouse:
    project = "billing-project"
    dataset = "p.d"

    def __init__(self):
        self.calls = []

    def dry_run(self, sql):
        self.calls.append("dry_run")
        return 0

    def run(self, sql):
        self.calls.append("run")
        return pd.DataFrame({"table_name": ["orders"], "row_count": [125000]})


def test_check_bigquery_dry_runs_then_runs():
    warehouse = FakeWarehouse()

    detail = health.check_bigquery(warehouse)

    assert warehouse.calls == ["dry_run", "run"]
    assert "billing-project" in detail
    assert "orders 125,000" in detail


def test_check_reply():
    reply = LLMReply(text=" OK\n", model="m", input_tokens=7, output_tokens=2, latency_s=0.4)
    llm = SimpleNamespace(generate=lambda prompt: reply)

    assert health.check_reply(llm) == 'replied "OK" in 0.4 s (7 tokens in, 2 out)'


def test_run_checks_reports_every_failure(monkeypatch):
    def no_credentials(settings):
        raise auth_errors.DefaultCredentialsError("No credentials found.")

    monkeypatch.setattr(health.Warehouse, "from_settings", no_credentials)

    results = {r.name: r for r in health.run_checks(load_settings())}

    assert results["Users file"].ok
    assert not results["BigQuery"].ok
    assert "gcloud auth" in results["BigQuery"].detail
    assert not results["Gemini"].ok
    assert "GEMINI_API_KEY is not set" in results["Gemini"].detail
