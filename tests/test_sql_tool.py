import pandas as pd
import pytest
from google.api_core import exceptions as gcp_errors

from da_assistant.sql_tool import (
    EMPTY_RESULT_HINT,
    SMALL_GROUPS_NOTE,
    QueryResult,
    SqlTool,
    format_bytes,
)

DS = "bigquery-public-data.thelook_ecommerce"
GOOD_SQL = f"SELECT brand FROM `{DS}.products`"


class FakeWarehouse:
    dataset = DS

    def __init__(self, *, estimate=100, rows=None, dry_run_error=None, run_error=None):
        self.estimate = estimate
        self.rows = pd.DataFrame({"brand": ["A", "B"]}) if rows is None else rows
        self.dry_run_error = dry_run_error
        self.run_error = run_error
        self.calls = []

    def dry_run(self, sql):
        self.calls.append(("dry_run", sql))
        if self.dry_run_error:
            raise self.dry_run_error
        return self.estimate

    def run(self, sql):
        self.calls.append(("run", sql))
        if self.run_error:
            raise self.run_error
        return self.rows


def run(warehouse, sql=GOOD_SQL, bytes_left=10_000):
    return SqlTool(warehouse, brands=None, max_result_rows=1000).run(sql, bytes_left=bytes_left)


def test_good_query_runs_after_a_dry_run():
    warehouse = FakeWarehouse()

    result = run(warehouse)

    assert result.useful
    assert result.bytes_scanned == 100
    assert [name for name, _ in warehouse.calls] == ["dry_run", "run"]
    # The checked SQL (with its row limit) is the one that runs.
    assert warehouse.calls[1][1] == result.sql
    assert result.sql.endswith("LIMIT 1000")


def test_rejected_query_never_reaches_bigquery():
    warehouse = FakeWarehouse()

    result = run(warehouse, sql=f"DROP TABLE `{DS}.orders`")

    assert result.error.startswith("Query rejected: ")
    assert warehouse.calls == []


def test_dry_run_error_goes_back_to_the_model():
    error = gcp_errors.BadRequest("POST https://x/y: Unrecognized name: revenue at [1:8]")
    warehouse = FakeWarehouse(dry_run_error=error)

    result = run(warehouse)

    assert result.error == "BigQuery: 400 Unrecognized name: revenue at [1:8]"
    assert [name for name, _ in warehouse.calls] == ["dry_run"]


def test_query_over_the_byte_budget_does_not_run():
    warehouse = FakeWarehouse(estimate=5_000_000)

    result = run(warehouse, bytes_left=1_000_000)

    assert "would scan 5.0 MB" in result.error
    assert "only 1.0 MB are left" in result.error
    assert [name for name, _ in warehouse.calls] == ["dry_run"]


def test_run_error_goes_back_to_the_model():
    warehouse = FakeWarehouse(run_error=gcp_errors.BadRequest("Division by zero"))

    result = run(warehouse)

    assert result.error == "BigQuery: 400 Division by zero"
    assert result.bytes_scanned == 100


def test_outage_is_raised_not_sent_to_the_model():
    # The model cannot fix an outage, so the caller handles it.
    warehouse = FakeWarehouse(run_error=gcp_errors.ServiceUnavailable("backend error"))

    with pytest.raises(gcp_errors.ServiceUnavailable):
        run(warehouse)


def test_empty_result_is_not_useful_and_has_a_hint():
    result = run(FakeWarehouse(rows=pd.DataFrame({"brand": []})))

    assert result.ok
    assert not result.useful
    assert result.for_model(50) == EMPTY_RESULT_HINT


def test_for_model_shows_csv_and_says_when_rows_are_cut():
    rows = pd.DataFrame({"brand": ["A", "B", "C"], "revenue": [1234.5678, 0.012345, 3.0]})

    text = QueryResult("SELECT 1", rows=rows).for_model(2)

    assert text.splitlines() == [
        "3 rows. Showing the first 2. Aggregate more if you need all of them.",
        "brand,revenue",
        "A,1234.57",
        "B,0.01235",
    ]


def test_people_queries_tell_the_model_that_small_groups_are_hidden():
    sql = f"SELECT state, COUNT(*) AS n FROM `{DS}.users` GROUP BY state"
    warehouse = FakeWarehouse(rows=pd.DataFrame({"state": ["California"], "n": [6]}))

    result = run(warehouse, sql=sql)

    assert result.hides_small_groups
    assert SMALL_GROUPS_NOTE in result.for_model(50).splitlines()[0]
    assert "HAVING COUNT(DISTINCT" in warehouse.calls[0][1]


def test_brand_scope_is_applied():
    warehouse = FakeWarehouse()

    SqlTool(warehouse, brands=("Diesel",), max_result_rows=10).run(GOOD_SQL, bytes_left=10_000)

    assert "brand IN ('Diesel')" in warehouse.calls[0][1]


def test_for_model_error():
    assert QueryResult("x", error="bad").for_model(50) == "ERROR: bad"


@pytest.mark.parametrize(
    ("count", "text"),
    [(0, "0 B"), (999, "999 B"), (6_200_000, "6.2 MB"), (1_500_000_000, "1.5 GB")],
)
def test_format_bytes(count, text):
    assert format_bytes(count) == text
