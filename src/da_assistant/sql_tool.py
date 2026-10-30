"""The `run_sql` tool: every query the model writes goes through here.

Steps: check and rewrite the SQL for the user (`sql_checks.py`), dry run it (free;
catches syntax errors and gives the bytes it will scan), check the byte budget, run it,
and turn the result into short text for the model. Errors the model can fix come back
as a result with `error` set, so the model can correct the query. Other errors
(auth, network, outages) are raised.
"""

from dataclasses import dataclass

import pandas as pd
from google.api_core import exceptions as gcp_errors

from da_assistant.errors import short_message
from da_assistant.sql_checks import MIN_GROUP_SIZE, SqlRejected, check_sql
from da_assistant.warehouse import Warehouse

# Errors caused by the query itself: bad syntax, unknown column, a limit hit.
_QUERY_ERRORS = (gcp_errors.BadRequest, gcp_errors.NotFound)

EMPTY_RESULT_HINT = (
    "The query ran and returned 0 rows. If it filters on text values (a country, "
    "a category, a status), check how the values are stored, for example with "
    "SELECT country, COUNT(*) ... GROUP BY country, before you try again."
)
SMALL_GROUPS_NOTE = (
    f"Groups with fewer than {MIN_GROUP_SIZE} customers are not shown, to protect privacy."
)


@dataclass(frozen=True)
class QueryResult:
    sql: str
    rows: pd.DataFrame | None = None
    bytes_scanned: int = 0
    error: str | None = None
    hides_small_groups: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def row_count(self) -> int:
        return 0 if self.rows is None else len(self.rows)

    @property
    def useful(self) -> bool:
        """The query ran and returned rows. Failed and empty queries count as retries."""
        return self.ok and self.row_count > 0

    def for_model(self, max_rows: int) -> str:
        if self.error:
            return f"ERROR: {self.error}"
        note = f" {SMALL_GROUPS_NOTE}" if self.hides_small_groups else ""
        if self.rows is None or self.rows.empty:
            return EMPTY_RESULT_HINT + note
        shown = self.rows.head(max_rows)
        header = f"{self.row_count} rows."
        if self.row_count > max_rows:
            header += f" Showing the first {max_rows}. Aggregate more if you need all of them."
        header += note
        return f"{header}\n{shown.to_csv(index=False, float_format=_format_float)}"


class SqlTool:
    def __init__(
        self, warehouse: Warehouse, *, brands: tuple[str, ...] | None, max_result_rows: int
    ) -> None:
        self.warehouse = warehouse
        # The brands the user can analyze. None means all brands.
        self.brands = brands
        self.max_result_rows = max_result_rows

    def run(self, sql: str, *, bytes_left: int) -> QueryResult:
        try:
            checked = check_sql(
                sql,
                dataset=self.warehouse.dataset,
                brands=self.brands,
                max_rows=self.max_result_rows,
            )
        except SqlRejected as exc:
            return QueryResult(sql, error=f"Query rejected: {exc}")
        sql = checked.sql

        try:
            estimate = self.warehouse.dry_run(sql)
        except _QUERY_ERRORS as exc:
            return QueryResult(sql, error=f"BigQuery: {short_message(exc, 500)}")
        if estimate > bytes_left:
            return QueryResult(
                sql,
                error=(
                    f"The query would scan {format_bytes(estimate)}, but only "
                    f"{format_bytes(bytes_left)} are left for this question. "
                    "Select fewer columns or a shorter time range."
                ),
            )

        try:
            rows = self.warehouse.run(sql)
        except _QUERY_ERRORS as exc:
            return QueryResult(
                sql, bytes_scanned=estimate, error=f"BigQuery: {short_message(exc, 500)}"
            )
        return QueryResult(
            sql, rows=rows, bytes_scanned=estimate, hides_small_groups=checked.hides_small_groups
        )


_UNITS = ("B", "KB", "MB", "GB", "TB")


def format_bytes(count: int) -> str:
    size, unit = float(count), 0
    while size >= 1000 and unit < len(_UNITS) - 1:
        size, unit = size / 1000, unit + 1
    return f"{count} B" if unit == 0 else f"{size:.1f} {_UNITS[unit]}"


def _format_float(value: float) -> str:
    # Money and counts read best with 2 decimals; small ratios need more digits.
    return f"{value:.2f}" if abs(value) >= 1 else f"{value:.4g}"
