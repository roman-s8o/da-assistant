"""Checks for every query the model writes, before it reaches BigQuery.

A query must be one read-only SELECT over the allowed tables. The checks also cap
the number of rows a query can return. A rejected query raises `SqlRejected` with
a reason the model can act on.
"""

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from da_assistant.schema import TABLES

_WRITES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Command,
)


class SqlRejected(ValueError):
    """The query breaks a rule. The message says which one."""


def check_sql(sql: str, *, dataset: str, max_rows: int) -> str:
    """Return the query to run, with a row limit, or raise `SqlRejected`."""
    query = _parse_one(sql)
    if not isinstance(query, exp.Query) or any(query.find_all(*_WRITES)):
        raise SqlRejected("Only read-only SELECT queries are allowed.")
    _check_tables(query, dataset)
    _cap_rows(query, max_rows)
    return query.sql(dialect="bigquery")


def _parse_one(sql: str) -> exp.Expression:
    try:
        statements = [s for s in sqlglot.parse(sql, read="bigquery") if s is not None]
    except ParseError as exc:
        first_line = str(exc).strip().splitlines()[0]
        raise SqlRejected(f"The query could not be parsed: {first_line}") from exc
    if len(statements) != 1:
        raise SqlRejected("Send exactly one SQL statement.")
    return statements[0]


def _check_tables(query: exp.Query, dataset: str) -> None:
    ctes = {cte.alias_or_name for cte in query.find_all(exp.CTE)}
    for table in query.find_all(exp.Table):
        is_cte = not table.db and not table.catalog and table.name in ctes
        is_allowed = f"{table.catalog}.{table.db}" == dataset and table.name in TABLES
        if not (is_cte or is_allowed):
            name = ".".join(part.name for part in table.parts)
            raise SqlRejected(
                f"Table `{name}` is not allowed. Use only {', '.join(TABLES)}, "
                f"with full names like `{dataset}.orders`."
            )


def _cap_rows(query: exp.Query, max_rows: int) -> None:
    limit = query.args.get("limit")
    value = limit.expression if isinstance(limit, exp.Limit) else None
    if isinstance(value, exp.Literal) and value.is_int and int(value.this) <= max_rows:
        return
    query.limit(max_rows, copy=False)
