"""Fakes shared by the tests of the agent: a scripted model, a guard and a SQL tool."""

from datetime import date
from pathlib import Path

import pandas as pd
from google.genai import errors as genai_errors
from langchain_core.messages import AIMessage

from da_assistant.agent import Agent, Budget
from da_assistant.config import UserProfile
from da_assistant.guard import GuardResult
from da_assistant.reports import ReportStore
from da_assistant.sql_tool import QueryResult

USER = UserProfile(id="ceo", name="CEO", all_brands=True)
ROWS = pd.DataFrame({"orders": [124452]})


def query_call(sql="SELECT 1", purpose="count orders", call_id="c1"):
    call = {"name": "run_sql", "args": {"sql": sql, "purpose": purpose}, "id": call_id}
    return AIMessage("", tool_calls=[{**call, "type": "tool_call"}])


def answer(text):
    usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    return AIMessage(text, usage_metadata=usage)


def ok(rows=ROWS, bytes_scanned=1000, hidden_groups=0):
    return QueryResult(
        "SELECT 1 LIMIT 1000",
        rows=rows,
        bytes_scanned=bytes_scanned,
        hides_small_groups=hidden_groups > 0,
        hidden_groups=hidden_groups,
    )


def failed(error="BigQuery: 400 Syntax error"):
    return QueryResult("SELEC 1", error=error)


class FakeModel:
    """Plays back scripted replies and records every call."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []  # (tools were bound, messages)

    def bind_tools(self, tools):
        return _WithTools(self)

    def invoke(self, messages, with_tools=False):
        self.calls.append((with_tools, list(messages)))
        return self.replies.pop(0)


class _WithTools:
    def __init__(self, model):
        self.model = model

    def invoke(self, messages):
        return self.model.invoke(messages, with_tools=True)

    @property
    def last_call(self):
        # Like ResilientChat: the copy with tools shares what the model records.
        return getattr(self.model, "last_call", None)


class FakeGuard:
    """Lets every message through, unless told to block it. Records what it saw."""

    model_name = "gemini-3.5-flash-lite"

    def __init__(self, category="analysis", llm_calls=0, source="model", error=None):
        self.category = category
        self.llm_calls = llm_calls
        self.source = source
        self.error = error
        self.calls = []  # (message, scope, last_answer)

    def check(self, message, *, scope, last_answer=""):
        self.calls.append((message, scope, last_answer))
        return GuardResult(
            self.category,
            "test",
            self.source,
            self.llm_calls,
            7 * self.llm_calls,
            self.llm_calls,
            error=self.error,
        )


class FakeSqlTool:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []  # (sql, bytes_left)

    def run(self, sql, *, bytes_left):
        self.calls.append((sql, bytes_left))
        return self.results.pop(0)


def make_agent(model, sql_tool, guard=None, reports=None, tracer=None, **limits):
    budget = Budget(**({"max_llm_calls": 8, "max_sql_retries": 2, "max_bytes": 10_000} | limits))
    return Agent(
        model=model,
        guard=guard or FakeGuard(),
        sql_tool=sql_tool,
        # The tests run in a temporary folder, so each test gets its own database.
        reports=reports or ReportStore(Path("reports.db")),
        user=USER,
        schema="THE SCHEMA",
        budget=budget,
        rows_for_model=50,
        tracer=tracer,
        today=lambda: date(2026, 10, 30),
    )


def last_message(model, call_index):
    return model.calls[call_index][1][-1]


def tool_call(name, call_id="r1", **args):
    return AIMessage(
        "", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}]
    )


def quota_error(period: str = "Day") -> genai_errors.ClientError:
    """A Gemini 429 like the real one: a quota per day ("Day") or per minute ("Minute")."""
    violation = {"quotaId": f"GenerateRequestsPer{period}PerProjectPerModel-FreeTier"}
    details = [{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [violation]}]
    body = {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED", "details": details}
    return genai_errors.ClientError(429, {"error": body})
