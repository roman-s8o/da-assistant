"""A small eval: ask the agent questions with known answers and score the answers.

Each question has reference SQL written by hand. The eval runs it to get the expected
value, asks the agent in a new conversation, and passes the question when a number in
the answer is within the tolerance of the expected value.
"""

import math
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from da_assistant.agent import Agent, TurnResult
from da_assistant.config import ConfigError, UserProfile
from da_assistant.errors import explain
from da_assistant.warehouse import Warehouse


class EvalQuestion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    user: str
    question: str
    sql: str
    expected: float
    as_of: date
    tolerance: float


@dataclass(frozen=True)
class EvalOutcome:
    question: EvalQuestion
    reference: float | None = None
    turn: TurnResult | None = None
    error: str | None = None

    @property
    def passed(self) -> bool:
        return (
            self.error is None
            and self.reference is not None
            and self.turn is not None
            and answer_matches(self.turn.answer, self.reference, self.question.tolerance)
        )

    @property
    def reference_changed(self) -> bool:
        """The data changed since the expected value was written down."""
        return (
            self.reference is not None
            and abs(self.reference - self.question.expected) > self.question.tolerance
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
    """Run every question. An error fails that question only."""
    agents: dict[str, Agent] = {}
    outcomes = []
    for question in questions:
        if on_question:
            on_question(question)
        try:
            reference = float(warehouse.run(question.sql).iloc[0, 0])
            if question.user not in agents:
                agents[question.user] = make_agent(users[question.user])
            turn = agents[question.user].ask(question.question, thread_id=uuid.uuid4().hex)
            outcomes.append(EvalOutcome(question, reference=reference, turn=turn))
        except Exception as exc:  # one broken question must not stop the eval
            outcomes.append(EvalOutcome(question, error=explain(exc)))
    return outcomes
