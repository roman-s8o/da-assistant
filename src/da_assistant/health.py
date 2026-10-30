"""Setup checks for `da-assistant check`: the users file, BigQuery and Gemini."""

import time
from collections.abc import Callable
from dataclasses import dataclass

from google import genai
from langchain_core.language_models import BaseChatModel

from da_assistant.config import ConfigError, Settings, load_users
from da_assistant.errors import explain
from da_assistant.llm import chat_model
from da_assistant.schema import TABLES
from da_assistant.warehouse import Warehouse


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def run_checks(settings: Settings) -> list[CheckResult]:
    """Run every check. A failed check does not stop the ones after it."""
    results = [
        _attempt("Users file", lambda: check_users(settings)),
        _attempt("BigQuery", lambda: check_bigquery(Warehouse.from_settings(settings))),
    ]
    try:
        model = chat_model(settings)
    except ConfigError as exc:
        return [*results, CheckResult("Gemini", False, str(exc))]
    fallback = settings.llm_fallback_model
    return [
        *results,
        _attempt(f"Gemini {settings.llm_model}", lambda: check_reply(model)),
        _attempt(
            f"Gemini {fallback} (fallback)",
            lambda: check_model_exists(model.client, fallback),
        ),
    ]


def check_users(settings: Settings) -> str:
    users = load_users(settings.users_file)
    return f"{len(users)} users in {settings.users_file}: {', '.join(users)}"


def row_count_sql(dataset: str) -> str:
    # COUNT(*) is answered from table metadata, so this query scans 0 bytes.
    return "\nUNION ALL\n".join(
        f"SELECT '{table}' AS table_name, COUNT(*) AS row_count FROM `{dataset}.{table}`"
        for table in TABLES
    )


def check_bigquery(warehouse: Warehouse) -> str:
    sql = row_count_sql(warehouse.dataset)
    scanned = warehouse.dry_run(sql)
    rows = warehouse.run(sql)
    counts = ", ".join(f"{row.table_name} {row.row_count:,}" for row in rows.itertuples())
    return f"project {warehouse.project}, dry run {scanned:,} bytes. Rows: {counts}"


def check_reply(model: BaseChatModel) -> str:
    start = time.perf_counter()
    reply = model.invoke("Reply with the single word OK.")
    seconds = time.perf_counter() - start
    usage = reply.usage_metadata or {}
    return (
        f'replied "{reply.text.strip()}" in {seconds:.1f} s '
        f"({usage.get('input_tokens', 0)} tokens in, {usage.get('output_tokens', 0)} out)"
    )


def check_model_exists(client: genai.Client, model: str) -> str:
    client.models.get(model=model)
    return "available"


def _attempt(name: str, check: Callable[[], str]) -> CheckResult:
    try:
        return CheckResult(name, True, check())
    except Exception as exc:  # a check reports any error instead of crashing
        return CheckResult(name, False, explain(exc))
