"""BigQuery access for the agent.

This wraps `BigQueryRunner` from `bq_client.py` (the client's file, kept unchanged).
The wrapper adds a free dry run and hard limits on bytes billed and run time.
"""

import pandas as pd
from google.cloud import bigquery

from da_assistant.bq_client import BigQueryRunner
from da_assistant.config import Settings


class Warehouse:
    def __init__(self, runner: BigQueryRunner, *, max_bytes_billed: int, timeout_s: float) -> None:
        self.runner = runner
        # The client applies this config to every job, including the ones started
        # by `BigQueryRunner.execute_query`, so the limits hold for all queries.
        runner.client.default_query_job_config = bigquery.QueryJobConfig(
            maximum_bytes_billed=max_bytes_billed,
            job_timeout_ms=int(timeout_s * 1000),
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> "Warehouse":
        runner = BigQueryRunner(project_id=settings.gcp_project, dataset_id=settings.dataset)
        return cls(
            runner,
            max_bytes_billed=settings.max_bytes_billed,
            timeout_s=settings.bq_timeout_s,
        )

    @property
    def project(self) -> str:
        """The project that pays for the jobs."""
        return self.runner.client.project

    @property
    def dataset(self) -> str:
        return self.runner.dataset_id

    def dry_run(self, sql: str) -> int:
        """Validate a query without running it. Returns the bytes it would scan.

        A dry run is free. It raises on syntax errors and on unknown tables or columns.
        """
        config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
        job = self.runner.client.query(sql, job_config=config)
        return job.total_bytes_processed or 0

    def run(self, sql: str) -> pd.DataFrame:
        return self.runner.execute_query(sql)
