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
from da_assistant.privacy import MASK, mask_rows
from da_assistant.sql_checks import HIDDEN_GROUPS_COLUMN, MIN_GROUP_SIZE, SqlRejected, check_sql
from da_assistant.warehouse import Warehouse

# Errors caused by the query itself: bad syntax, unknown column, a limit hit.
_QUERY_ERRORS = (gcp_errors.BadRequest, gcp_errors.NotFound)

EMPTY_RESULT_HINT = (
    "The query ran and returned 0 rows. If it filters on text values (a country, "
    "a category, a status), check how the values are stored, for example with "
    "SELECT country, COUNT(*) ... GROUP BY country, before you try again."
)


def hidden_groups_note(count: int) -> str:
    """One sentence about the groups that the minimum group size removed."""
    if count == 0:
        return f"No group had fewer than {MIN_GROUP_SIZE} customers, so nothing was hidden."
    groups = "1 group" if count == 1 else f"{count} groups"
    verb = "was" if count == 1 else "were"
    return f"{groups} with fewer than {MIN_GROUP_SIZE} customers {verb} hidden to protect privacy."


def hidden_groups_summary(counts: list[int]) -> str:
    """The privacy note for an answer built on one or more results that hid groups.

    Counts are not added up: two queries over the same groups hide the same groups.
    """
    distinct = sorted(set(counts), reverse=True)
    if len(distinct) == 1:
        return hidden_groups_note(distinct[0])
    numbers = ", ".join(str(n) for n in counts[:-1]) + f" and {counts[-1]}"
    return (
        f"Groups with fewer than {MIN_GROUP_SIZE} customers were hidden to protect privacy "
        f"({numbers} groups in {len(counts)} results)."
    )


@dataclass(frozen=True)
class QueryResult:
    sql: str
    rows: pd.DataFrame | None = None
    bytes_scanned: int = 0
    error: str | None = None
    # For queries about people: groups removed because they are too small.
    hides_small_groups: bool = False
    hidden_groups: int = 0
    # Values that looked like personal data and were masked (should always be 0).
    masked_values: int = 0

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def row_count(self) -> int:
        return 0 if self.rows is None else len(self.rows)

    @property
    def useful(self) -> bool:
        """The query gave an answer. Failed and empty queries count as retries.

        A result with every group hidden is an answer too: retrying cannot change it.
        """
        return self.ok and (self.row_count > 0 or self.hidden_groups > 0)

    def for_model(self, max_rows: int) -> str:
        if self.error:
            return f"ERROR: {self.error}"
        notes = []
        if self.hides_small_groups:
            notes.append(hidden_groups_note(self.hidden_groups))
        if self.masked_values:
            notes.append(
                f"{self.masked_values} values looked like personal data and were masked as {MASK}."
            )
        if self.rows is None or self.rows.empty:
            if self.hidden_groups:
                return (
                    f"The query returned no rows: every group has fewer than {MIN_GROUP_SIZE} "
                    f"customers (maybe none). {' '.join(notes)} If a text filter may be "
                    "misspelled, check the stored values; otherwise tell the user, and do "
                    "not try to get around it."
                )
            return " ".join([EMPTY_RESULT_HINT, *notes])
        shown = self.rows.head(max_rows)
        header = f"{self.row_count} rows."
        if self.row_count > max_rows:
            header += f" Showing the first {max_rows}. Aggregate more if you need all of them."
        header = " ".join([header, *notes])
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
            hidden = 0
            if checked.hidden_groups_sql is not None:
                rows, hidden, extra_bytes = self._take_hidden_groups(
                    rows, checked.hidden_groups_sql
                )
                estimate += extra_bytes
        except _QUERY_ERRORS as exc:
            return QueryResult(
                sql, bytes_scanned=estimate, error=f"BigQuery: {short_message(exc, 500)}"
            )
        rows, masked = mask_rows(rows)
        return QueryResult(
            sql,
            rows=rows,
            bytes_scanned=estimate,
            hides_small_groups=checked.hides_small_groups,
            hidden_groups=hidden,
            masked_values=masked,
        )

    def _take_hidden_groups(
        self, rows: pd.DataFrame, count_sql: str
    ) -> tuple[pd.DataFrame, int, int]:
        """Take the number of hidden groups out of the result.

        Returns the rows without the count column, the count, and the extra bytes scanned.
        """
        if HIDDEN_GROUPS_COLUMN in rows.columns:
            count = None if rows.empty else int(rows[HIDDEN_GROUPS_COLUMN].iloc[0])
            rows = rows.drop(columns=HIDDEN_GROUPS_COLUMN)
            if count is not None:
                return rows, count, 0
        # No row came back to carry the count, so ask for the count alone.
        extra_bytes = self.warehouse.dry_run(count_sql)
        return rows, int(self.warehouse.run(count_sql).iloc[0, 0]), extra_bytes


_UNITS = ("B", "KB", "MB", "GB", "TB")


def format_bytes(count: int) -> str:
    size, unit = float(count), 0
    while size >= 1000 and unit < len(_UNITS) - 1:
        size, unit = size / 1000, unit + 1
    return f"{count} B" if unit == 0 else f"{size:.1f} {_UNITS[unit]}"


def _format_float(value: float) -> str:
    # Money and counts read best with 2 decimals; small ratios need more digits.
    return f"{value:.2f}" if abs(value) >= 1 else f"{value:.4g}"
