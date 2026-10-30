"""Setup checks for `da-assistant check`: the users file, BigQuery and Gemini."""

import re
from collections.abc import Callable
from dataclasses import dataclass

from google.api_core import exceptions as gcp_errors
from google.auth import exceptions as auth_errors
from google.genai import errors as genai_errors

from da_assistant.config import ConfigError, Settings, load_users
from da_assistant.llm import Gemini
from da_assistant.warehouse import Warehouse

TABLES = ("orders", "order_items", "products", "users")

# Google API errors start with the HTTP method and URL, which only add noise for the user.
_REQUEST_PREFIX = re.compile(r"(GET|POST|PUT|PATCH|DELETE) \S+: ")


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
        llm = Gemini.from_settings(settings)
    except ConfigError as exc:
        return [*results, CheckResult("Gemini", False, str(exc))]
    return [
        *results,
        _attempt(f"Gemini {llm.model}", lambda: check_reply(llm)),
        _attempt(
            f"Gemini {llm.fallback_model} (fallback)",
            lambda: check_model_exists(llm, llm.fallback_model),
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


def check_reply(llm: Gemini) -> str:
    reply = llm.generate("Reply with the single word OK.")
    return (
        f'replied "{reply.text.strip()}" in {reply.latency_s:.1f} s '
        f"({reply.input_tokens} tokens in, {reply.output_tokens} out)"
    )


def check_model_exists(llm: Gemini, model: str) -> str:
    llm.client.models.get(model=model)
    return "available"


def _attempt(name: str, check: Callable[[], str]) -> CheckResult:
    try:
        return CheckResult(name, True, check())
    except Exception as exc:  # a check reports any error instead of crashing
        return CheckResult(name, False, explain(exc))


def explain(exc: Exception) -> str:
    """A short error message with a hint on how to fix it, when we know one."""
    if isinstance(exc, ConfigError):
        return str(exc)
    message = _short_message(exc)
    hint = _hint(exc)
    return f"{message}\nHint: {hint}" if hint else message


def _short_message(exc: Exception) -> str:
    if isinstance(exc, genai_errors.APIError) and exc.message:
        text = f"{exc.code} {exc.status}: {exc.message}"
    else:
        text = _REQUEST_PREFIX.sub("", str(exc))
    lines = text.strip().splitlines()
    return lines[0][:300] if lines else type(exc).__name__


def _hint(exc: Exception) -> str | None:
    if isinstance(exc, auth_errors.GoogleAuthError):
        return "run `gcloud auth application-default login`."
    text = str(exc)
    if "has not enabled BigQuery" in text or "BigQuery API has not been used" in text:
        return (
            "enable the BigQuery API in this project, "
            "or set DA_GCP_PROJECT to a project where it is enabled."
        )
    if isinstance(exc, gcp_errors.Forbidden):
        return (
            "check that the BigQuery API is enabled in the billing project "
            "and that your account can run jobs there."
        )
    if isinstance(exc, genai_errors.APIError):
        if exc.code in (401, 403) or (exc.code == 400 and "API key" in text):
            return "check GEMINI_API_KEY."
        if exc.code == 404:
            return "model not found. Set DA_LLM_MODEL or DA_LLM_FALLBACK_MODEL."
        if exc.code == 429:
            return "rate limit or quota reached. Wait a minute and try again."
        if exc.code >= 500:
            return "Gemini service error. Try again later."
    return None
