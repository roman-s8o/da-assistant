"""Checks and rewrites for every query the model writes, before it reaches BigQuery.

`check_sql` returns the query to run, or raises `SqlRejected` with a reason the model
can act on. The rules, in order:

1. One read-only SELECT over the four allowed tables. No table functions.
2. No `*` (except in COUNT(*)) and no whole table rows as values: list the columns.
3. No direct personal data: names, email, street address, postal code, coordinates.
4. A brand manager cannot filter on brands outside their scope (a clear message for
   the model; the safe views enforce the scope anyway).
5. Columns about people (city, state, country, age, gender, traffic_source) only in the
   final SELECT, which must group or aggregate. Customer ids may appear there only
   inside COUNT or in JOIN conditions. Groups with fewer than 5 customers are removed
   inside BigQuery, and the query also returns how many groups were removed.
6. Every table is replaced with its safe view (`views.py`): only allowed columns,
   only the user's brands. This is the guarantee for rules 3 and 4.
7. A limit on the number of rows.
"""

from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError
from sqlglot.optimizer.scope import Scope, build_scope

from da_assistant.schema import DIRECT_PII, PEOPLE_COLUMNS, TABLES, USER_KEYS
from da_assistant.views import apply_views

MIN_GROUP_SIZE = 5

# A column added to queries about people: the number of groups removed because they
# have fewer than MIN_GROUP_SIZE customers. SqlTool takes it out of the result.
HIDDEN_GROUPS_COLUMN = "_hidden_groups"

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

# Aggregates that summarize a group. The others (ARRAY_AGG, STRING_AGG, ANY_VALUE,
# MAX_BY and so on) can return the values of single rows.
_GROUP_SUMMARIES = (
    exp.Count,
    exp.CountIf,
    exp.Sum,
    exp.Avg,
    exp.Min,
    exp.Max,
    exp.ApproxDistinct,
    exp.ApproxQuantiles,
    exp.Stddev,
    exp.StddevPop,
    exp.StddevSamp,
    exp.Variance,
    exp.VariancePop,
    exp.LogicalAnd,
    exp.LogicalOr,
    exp.Corr,
    exp.CovarPop,
    exp.CovarSamp,
)

_PEOPLE = "city, state, country, age, gender or traffic_source"

_TWO_WAYS = (
    "There are two allowed ways: list single customers by customer id without these "
    "columns (for example top customers by spend), or group by these columns without "
    "customer ids (for example customers and revenue by state)."
)


class SqlRejected(ValueError):
    """The query breaks a rule. The message says which one and how to fix it."""


@dataclass(frozen=True)
class CheckedSql:
    sql: str
    # Only for queries about people: a query that returns just the number of hidden
    # groups. The main query returns it too, in HIDDEN_GROUPS_COLUMN, but when every
    # group is hidden there is no row to carry it.
    hidden_groups_sql: str | None = None

    @property
    def hides_small_groups(self) -> bool:
        return self.hidden_groups_sql is not None


def check_sql(
    sql: str, *, dataset: str, brands: tuple[str, ...] | None, max_rows: int
) -> CheckedSql:
    """Check a query and rewrite it for the user. `brands=None` means all brands."""
    query = _parse_one(sql)
    if not isinstance(query, exp.Query) or any(query.find_all(*_WRITES)):
        raise SqlRejected("Only read-only SELECT queries are allowed.")
    _check_tables(query, dataset)
    _check_columns(query)
    if brands is not None:
        _check_brand_filters(query, brands)
    hidden_groups = _protect_people(query)
    hidden_groups_sql = None
    if hidden_groups is not None:
        hidden_groups_sql = _hidden_groups_query(query, hidden_groups, dataset, brands)
    apply_views(query, dataset, brands)
    _cap_rows(query, max_rows)
    return CheckedSql(query.sql(dialect="bigquery"), hidden_groups_sql)


def _hidden_groups_query(
    query: exp.Query, count: exp.Expression, dataset: str, brands: tuple[str, ...] | None
) -> str:
    """Add the count of hidden groups to every final SELECT, and return it as its own query.

    Inside the same query the count costs nothing extra: BigQuery bills the columns of a
    table once per query, even when the query reads the table twice.
    """
    for select in _final_select_nodes(query):
        select.select(exp.alias_(count.copy(), HIDDEN_GROUPS_COLUMN), copy=False)
    alone = exp.select(exp.alias_(count.copy(), "hidden_groups"))
    if not isinstance(query, exp.Select) and query.args.get("with_"):
        # A UNION keeps its WITH clause at the top; the count needs it too.
        alone.set("with_", query.args["with_"].copy())
    apply_views(alone, dataset, brands)
    return alone.sql(dialect="bigquery")


def _parse_one(sql: str) -> exp.Expression:
    try:
        statements = [s for s in sqlglot.parse(sql, read="bigquery") if s is not None]
    except ParseError as exc:
        first_line = str(exc).strip().splitlines()[0]
        raise SqlRejected(f"The query could not be parsed: {first_line}") from exc
    if len(statements) != 1:
        raise SqlRejected("Send exactly one SQL statement.")
    query = statements[0]
    while isinstance(query, exp.Subquery):  # (SELECT ...) as a whole statement
        query = query.this
    return query


def _check_tables(query: exp.Query, dataset: str) -> None:
    ctes = {cte.alias_or_name for cte in query.find_all(exp.CTE)}
    for table in query.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            raise SqlRejected("Table functions are not allowed. Query the tables directly.")
        is_cte = not table.db and not table.catalog and table.name in ctes
        is_allowed = f"{table.catalog}.{table.db}" == dataset and table.name in TABLES
        if not (is_cte or is_allowed):
            name = ".".join(part.name for part in table.parts)
            raise SqlRejected(
                f"Table `{name}` is not allowed. Use only {', '.join(TABLES)}, "
                f"with full names like `{dataset}.orders`."
            )


def _check_columns(query: exp.Query) -> None:
    for star in query.find_all(exp.Star):
        if not isinstance(star.parent, exp.Count):
            raise SqlRejected("SELECT * is not allowed. List the columns you need.")
    for identifier in query.find_all(exp.Identifier):
        if identifier.name.lower() == HIDDEN_GROUPS_COLUMN:
            raise SqlRejected(f"`{identifier.name}` is a reserved name. Use another name.")
        if identifier.name.lower() in DIRECT_PII:
            raise SqlRejected(
                f"`{identifier.name}` is personal data and cannot be used. "
                "Names, emails, addresses, postal codes and coordinates are never available."
            )
    for scope in build_scope(query).traverse():
        for column in scope.columns:
            source = scope.sources.get(column.name)
            if not column.table and isinstance(source, exp.Table) and source.db:
                raise SqlRejected(
                    f"`{column.name}` is a whole table row. List the columns you need instead."
                )


def _check_brand_filters(query: exp.Query, brands: tuple[str, ...]) -> None:
    allowed = {brand.lower() for brand in brands}
    for node in query.find_all(exp.EQ, exp.In):
        for value in _brand_values(node):
            if value.lower() not in allowed:
                raise SqlRejected(
                    f"The user can analyze only these brands: {', '.join(brands)}. "
                    f"'{value}' is not one of them. Do not query it; tell the user."
                )


def _brand_values(node: exp.Expression) -> list[str]:
    """The string values a brand column is compared with in `brand = '...'` or `brand IN`."""

    def is_brand(side: exp.Expression) -> bool:
        return isinstance(side, exp.Column) and side.name.lower() == "brand"

    def is_text(side: exp.Expression) -> bool:
        return isinstance(side, exp.Literal) and side.is_string

    if isinstance(node, exp.EQ):
        for column, value in ((node.this, node.expression), (node.expression, node.this)):
            if is_brand(column) and is_text(value):
                return [value.this]
    if isinstance(node, exp.In) and is_brand(node.this):
        return [value.this for value in node.expressions if is_text(value)]
    return []


def _protect_people(query: exp.Query) -> exp.Expression | None:
    """Apply rule 5.

    Returns None when the query does not use columns about people. Otherwise returns an
    expression that counts the groups the query hides.
    """
    root = build_scope(query)
    final_selects = _final_selects(root)
    counts = []
    for scope in root.traverse():
        if not any(column.name.lower() in PEOPLE_COLUMNS for column in scope.columns):
            continue
        if not any(scope is final for final in final_selects):
            raise SqlRejected(
                f"Use {_PEOPLE} only in the final SELECT, not in a WITH clause or a "
                "subquery. Join users or orders in the final SELECT instead. "
                f"{_TWO_WAYS}"
            )
        counts.append(_check_people_select(scope))
    if not counts:
        return None
    total = counts[0]
    for count in counts[1:]:
        total = exp.Add(this=total, expression=count)
    return total


def _final_selects(scope: Scope) -> list[Scope]:
    """The SELECTs whose rows are the result: the root, or each branch of a UNION."""
    if isinstance(scope.expression, exp.Select):
        return [scope]
    return [select for child in scope.set_operation_scopes for select in _final_selects(child)]


def _final_select_nodes(query: exp.Query) -> list[exp.Select]:
    if isinstance(query, exp.Select):
        return [query]
    if isinstance(query, exp.Subquery):
        return _final_select_nodes(query.this)
    return _final_select_nodes(query.this) + _final_select_nodes(query.expression)


def _check_people_select(scope: Scope) -> exp.Expression:
    """Check a final SELECT that uses columns about people, and remove its small groups.

    Returns a scalar subquery that counts the groups it removes.
    """
    select = scope.expression
    if not select.args.get("group") and not _only_aggregates(select):
        raise SqlRejected(
            f"A query that uses {_PEOPLE} must group the rows (GROUP BY) or return only "
            "aggregates like COUNT, SUM or AVG. To see the values of a column, use for "
            "example SELECT state, COUNT(*) ... GROUP BY state."
        )
    for node in _own_nodes(select):
        if isinstance(node, exp.AggFunc) and not isinstance(node, _GROUP_SUMMARIES):
            raise SqlRejected(
                f"{node.sql_name()} can return values of single rows, so it cannot be used "
                f"with {_PEOPLE}. Use COUNT, SUM, AVG, MIN or MAX."
            )
    for column in scope.columns:
        if _is_customer_id(column, scope) and not _counted_or_joined(column):
            raise SqlRejected(
                f"Customer ids cannot be shown, grouped or filtered next to {_PEOPLE}. "
                f"Here customer ids may appear only as COUNT(DISTINCT ...) or in "
                f"JOIN ... ON a = b. {_TWO_WAYS}"
            )
    key = _customer_key(scope)
    if key is None:
        raise SqlRejected(
            f"A query that uses {_PEOPLE} must read users, orders or order_items directly "
            "in its final SELECT, so that groups with few customers can be hidden."
        )
    customers = exp.Count(this=exp.Distinct(expressions=[key]))
    size = exp.Literal.number(MIN_GROUP_SIZE)

    # The same SELECT with only the small groups: count its rows. The SELECT's own HAVING,
    # ORDER BY, LIMIT and QUALIFY are dropped, so every group with fewer customers is
    # counted, also when the model already filtered them out itself.
    small_groups = select.copy()
    for clause in ("having", "order", "limit", "offset", "qualify"):
        small_groups.set(clause, None)
    small_groups.having(exp.LT(this=customers.copy(), expression=size.copy()), copy=False)
    count = exp.select(exp.Count(this=exp.Star())).from_(small_groups.subquery("small_groups"))

    # Remove groups with fewer than MIN_GROUP_SIZE customers.
    select.having(exp.GTE(this=customers, expression=size), copy=False)
    return count.subquery()


def _own_nodes(select: exp.Select):
    """The nodes of a SELECT, without the nodes of its subqueries and WITH clause."""
    return select.walk(prune=lambda node: node is not select and isinstance(node, exp.Query))


def _only_aggregates(select: exp.Select) -> bool:
    """Every column of the SELECT list is inside an aggregate (not a window)."""
    has_aggregate = False
    for projection in select.expressions:
        for node in projection.walk(prune=lambda n: isinstance(n, exp.Query)):
            if isinstance(node, exp.Column) and not _inside_aggregate(node):
                return False
            if isinstance(node, exp.AggFunc) and not isinstance(node.parent, exp.Window):
                has_aggregate = True
    return has_aggregate


def _inside_aggregate(column: exp.Column) -> bool:
    node = column.parent
    while node is not None and not isinstance(node, exp.Select):
        if isinstance(node, exp.AggFunc):
            return not isinstance(node.parent, exp.Window)
        node = node.parent
    return False


def _is_customer_id(column: exp.Column, scope: Scope) -> bool:
    name = column.name.lower()
    if name == "user_id":
        return True
    if name != "id":
        return False
    if column.table:
        source = scope.sources.get(column.table)
        # An `id` from a WITH clause or a subquery may be a customer id.
        return not isinstance(source, exp.Table) or source.name == "users"
    tables = [s for s in scope.sources.values() if isinstance(s, exp.Table)]
    only_other_table = len(scope.sources) == 1 and tables and tables[0].name != "users"
    return not only_other_table


def _counted_or_joined(column: exp.Column) -> bool:
    """The column is COUNT(id), COUNT(DISTINCT id), or one side of JOIN ... ON a = b."""
    parent = column.parent
    if isinstance(parent, exp.Distinct):
        parent = parent.parent
    if isinstance(parent, (exp.Count, exp.ApproxDistinct)):
        return True
    if not (isinstance(parent, exp.EQ) and isinstance(parent.this, exp.Column)):
        return False
    if not isinstance(parent.expression, exp.Column):
        return False
    node = parent
    while isinstance(node.parent, (exp.And, exp.Paren)):
        node = node.parent
    return node.arg_key == "on" and isinstance(node.parent, exp.Join)


def _customer_key(scope: Scope) -> exp.Column | None:
    """The customer id column of a table read directly by this SELECT."""
    for table_name in ("users", "order_items", "orders"):
        for alias, (_, source) in scope.selected_sources.items():
            if isinstance(source, exp.Table) and source.db and source.name == table_name:
                return exp.column(USER_KEYS[table_name], table=alias)
    return None


def _cap_rows(query: exp.Query, max_rows: int) -> None:
    limit = query.args.get("limit")
    value = limit.expression if isinstance(limit, exp.Limit) else None
    if isinstance(value, exp.Literal) and value.is_int and int(value.this) <= max_rows:
        return
    query.limit(max_rows, copy=False)
