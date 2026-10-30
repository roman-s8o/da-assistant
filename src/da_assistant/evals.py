"""A small eval: ask the agent questions and check what it does.

A case can check a number and the agent's behavior:
- A known number: reference SQL written by hand. The eval runs it to get the expected value
  and passes the check when a number in the answer is within the tolerance.
- Behavior: at most N model calls, at most N results with every group hidden, words the
  answer must mention.

Each case runs in a new conversation. The report shows which model answered each case,
because the primary model can fall back to another one.
"""

import math
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Self

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from da_assistant.agent import Agent, TurnResult
from da_assistant.config import ConfigError, UserProfile
from da_assistant.errors import explain
from da_assistant.warehouse import Warehouse


class EvalQuestion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    user: str
    question: str
    # What the case is about, for the report: for example "time", "products", "privacy".
    category: str = ""
    # A known number: the reference SQL gives it; `expected` is its value on `as_of`.
    sql: str | None = None
    expected: float | None = None
    as_of: date | None = None
    tolerance: float = 0.0
    # Checks on the agent's behavior.
    max_model_calls: int | None = None
    max_all_hidden_results: int | None = None
    answer_mentions_any: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _check_case(self) -> Self:
        number_fields = (self.sql, self.expected, self.as_of)
        if any(f is not None for f in number_fields) and None in number_fields:
            raise ValueError("a known number needs sql, expected and as_of together")
        if self.sql is None and not self._behavior_checks():
            raise ValueError("a case needs a known number or at least one behavior check")
        return self

    def _behavior_checks(self) -> bool:
        return (
            self.max_model_calls is not None
            or self.max_all_hidden_results is not None
            or bool(self.answer_mentions_any)
        )


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class EvalOutcome:
    question: EvalQuestion
    reference: float | None = None
    turn: TurnResult | None = None
    # The turn record of the trace: models, fallback, hidden groups.
    record: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def checks(self) -> list[Check]:
        if self.error is not None or self.turn is None:
            return [Check("ran", False, self.error or "no answer")]
        q, turn = self.question, self.turn
        checks = []
        if q.sql is not None and self.reference is not None:
            checks.append(
                Check(
                    "number",
                    answer_matches(turn.answer, self.reference, q.tolerance),
                    f"expected {self.reference:,.2f}",
                )
            )
        if q.max_model_calls is not None:
            checks.append(
                Check(
                    "model calls",
                    turn.llm_calls <= q.max_model_calls,
                    f"{turn.llm_calls}, at most {q.max_model_calls}",
                )
            )
        if q.max_all_hidden_results is not None:
            count = self.record.get("all_hidden_results", 0)
            checks.append(
                Check(
                    "fully hidden results",
                    count <= q.max_all_hidden_results,
                    f"{count}, at most {q.max_all_hidden_results}",
                )
            )
        if q.answer_mentions_any:
            text = turn.answer.casefold()
            found = [word for word in q.answer_mentions_any if word.casefold() in text]
            checks.append(
                Check(
                    "mentions",
                    bool(found),
                    f"found {', '.join(found)}"
                    if found
                    else "none of: " + ", ".join(q.answer_mentions_any),
                )
            )
        return checks

    @property
    def passed(self) -> bool:
        checks = self.checks
        return bool(checks) and all(check.passed for check in checks)

    @property
    def model(self) -> str:
        """The model that answered, and whether it was the fallback model."""
        models = ", ".join(self.record.get("models", [])) or "?"
        return models + (" (fallback)" if self.record.get("fallback") else "")

    @property
    def reference_changed(self) -> bool:
        """The data changed since the expected value was written down."""
        q = self.question
        return (
            self.reference is not None
            and q.expected is not None
            and abs(self.reference - q.expected) > q.tolerance
        )


def load_questions(path: Path) -> list[EvalQuestion]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return [EvalQuestion(**item) for item in data["questions"]]
    except FileNotFoundError as exc:
        raise ConfigError(f"Eval file not found: {path}") from exc
    except (yaml.YAMLError, KeyError, TypeError, ValidationError) as exc:
        raise ConfigError(f"Eval file {path} is invalid: {exc}") from exc


# A number as people write it: 8025, 8,025, $8,024.59, $1.2M, 38.5%.
_NUMBER = re.compile(r"(?<![\w.])\$?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s?([KMB])?(?![\w])")
_SCALE = {"K": 1e3, "M": 1e6, "B": 1e9}


def numbers_in(text: str) -> list[float]:
    numbers = []
    for whole, fraction, suffix in _NUMBER.findall(text):
        value = float(whole.replace(",", "") + fraction)
        numbers.append(value * _SCALE.get(suffix, 1))
    return numbers


def answer_matches(answer: str, expected: float, tolerance: float) -> bool:
    return any(abs(number - expected) <= tolerance for number in numbers_in(answer))


def wilson_interval(passed: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """The 95% confidence interval of the pass rate. Honest for small question sets."""
    if total == 0:
        return (0.0, 1.0)
    rate = passed / total
    center = (rate + z * z / (2 * total)) / (1 + z * z / total)
    spread = z * math.sqrt(rate * (1 - rate) / total + z * z / (4 * total * total))
    spread /= 1 + z * z / total
    return (max(0.0, center - spread), min(1.0, center + spread))


def run_eval(
    questions: list[EvalQuestion],
    *,
    users: dict[str, UserProfile],
    warehouse: Warehouse,
    make_agent: Callable[[UserProfile], Agent],
    on_question: Callable[[EvalQuestion], None] | None = None,
) -> list[EvalOutcome]:
    """Run every case. An error fails that case only."""
    agents: dict[str, Agent] = {}
    outcomes = []
    for question in questions:
        if on_question:
            on_question(question)
        try:
            reference = None
            if question.sql is not None:
                reference = float(warehouse.run(question.sql).iloc[0, 0])
            if question.user not in agents:
                agents[question.user] = make_agent(users[question.user])
            agent = agents[question.user]
            turn = agent.ask(question.question, thread_id=uuid.uuid4().hex)
            trace = getattr(agent, "last_trace", None)
            record = trace.record if trace is not None else {}
            outcomes.append(EvalOutcome(question, reference=reference, turn=turn, record=record))
        except Exception as exc:  # one broken case must not stop the eval
            outcomes.append(EvalOutcome(question, error=explain(exc)))
    return outcomes
