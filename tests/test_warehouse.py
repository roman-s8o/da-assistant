import pandas as pd

from da_assistant.warehouse import Warehouse


class FakeJob:
    total_bytes_processed = 1234


class FakeClient:
    project = "billing-project"

    def __init__(self):
        self.default_query_job_config = None
        self.queries = []

    def query(self, sql, job_config=None):
        self.queries.append((sql, job_config))
        return FakeJob()


class FakeRunner:
    """Stands in for BigQueryRunner, which needs real credentials."""

    dataset_id = "project.dataset"

    def __init__(self):
        self.client = FakeClient()
        self.executed = []

    def execute_query(self, sql):
        self.executed.append(sql)
        return pd.DataFrame({"n": [1]})


def make_warehouse():
    runner = FakeRunner()
    return Warehouse(runner, max_bytes_billed=500, timeout_s=2.5), runner


def test_limits_apply_to_every_job():
    _, runner = make_warehouse()

    config = runner.client.default_query_job_config
    assert config.maximum_bytes_billed == 500
    # The library keeps int64 values as strings, like the REST API does.
    assert int(config.job_timeout_ms) == 2500


def test_dry_run_returns_bytes_without_running():
    warehouse, runner = make_warehouse()

    assert warehouse.dry_run("SELECT 1") == 1234

    sql, config = runner.client.queries[0]
    assert sql == "SELECT 1"
    assert config.dry_run is True
    assert config.use_query_cache is False
    assert runner.executed == []


def test_run_uses_the_client_runner():
    warehouse, runner = make_warehouse()

    result = warehouse.run("SELECT 1")

    assert runner.executed == ["SELECT 1"]
    assert result["n"].tolist() == [1]


def test_project_and_dataset():
    warehouse, _ = make_warehouse()

    assert warehouse.project == "billing-project"
    assert warehouse.dataset == "project.dataset"
