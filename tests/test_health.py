from types import SimpleNamespace

import pandas as pd
from google.auth import exceptions as auth_errors
from langchain_core.messages import AIMessage

from da_assistant import health
from da_assistant.config import load_settings


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


def test_row_count_sql_covers_all_tables():
    sql = health.row_count_sql("p.d")

    for table in health.TABLES:
        assert f"`p.d.{table}`" in sql


def test_check_bigquery_dry_runs_then_runs():
    warehouse = FakeWarehouse()

    detail = health.check_bigquery(warehouse)

    assert warehouse.calls == ["dry_run", "run"]
    assert "billing-project" in detail
    assert "orders 125,000" in detail


def test_check_reply():
    reply = AIMessage(
        " OK\n", usage_metadata={"input_tokens": 7, "output_tokens": 2, "total_tokens": 9}
    )
    model = SimpleNamespace(invoke=lambda prompt: reply)

    detail = health.check_reply(model)

    assert detail.startswith('replied "OK" in ')
    assert detail.endswith("(7 tokens in, 2 out)")


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
