import pandas as pd
import pytest

from da_assistant.agent import TurnResult
from da_assistant.config import ConfigError, load_users
from da_assistant.evals import (
    EvalQuestion,
    answer_matches,
    load_questions,
    numbers_in,
    run_eval,
    wilson_interval,
)
from da_assistant.sql_checks import check_sql

DS = "bigquery-public-data.thelook_ecommerce"


def test_shipped_questions_are_valid(questions_file, users_file):
    questions = load_questions(questions_file)
    users = load_users(users_file)

    assert len({q.id for q in questions}) == len(questions)
    for question in questions:
        assert question.user in users
        # The reference SQL must be a read-only query over the allowed tables.
        check_sql(question.sql, dataset=DS, brands=users[question.user].brand_scope, max_rows=1000)


def test_diesel_case_is_in_the_set(questions_file):
    diesel = next(q for q in load_questions(questions_file) if q.id == "diesel-revenue-2026-09")

    assert "oi.created_at" in diesel.sql
    assert answer_matches(
        "Diesel made $8,025 in September 2026.", diesel.expected, diesel.tolerance
    )
    # The wrong definition (sales dated by orders.created_at) must fail.
    assert not answer_matches("Diesel made $8,011.", diesel.expected, diesel.tolerance)


@pytest.mark.parametrize(
    ("text", "numbers"),
    [
        ("Revenue was $8,024.59.", [8024.59]),
        ("$8,025 and 8025", [8025.0, 8025.0]),
        ("$1.2M, $8.0K and 38.5%", [1_200_000.0, 8000.0, 38.5]),
        ("Q3 2026 had 5 Men's brands", [2026.0, 5.0]),
        ("no numbers here", []),
    ],
)
def test_numbers_in(text, numbers):
    assert numbers_in(text) == pytest.approx(numbers)


def test_rounded_answer_does_not_pass_a_tight_tolerance():
    assert not answer_matches("About $8.0K.", 8024.59, 1.0)


def test_wilson_interval():
    low, high = wilson_interval(1, 1)

    assert high == 1.0
    assert 0.2 < low < 0.21
    assert wilson_interval(0, 0) == (0.0, 1.0)


def test_bad_eval_file(tmp_path):
    path = tmp_path / "questions.yaml"
    path.write_text("questions:\n  - id: x\n")

    with pytest.raises(ConfigError, match="invalid"):
        load_questions(path)


QUESTION = EvalQuestion(
    id="q1",
    user="ceo",
    question="What was the revenue?",
    sql="SELECT 1",
    expected=100.0,
    as_of="2026-10-30",
    tolerance=1.0,
)


class FakeWarehouse:
    def __init__(self, value):
        self.value = value

    def run(self, sql):
        return pd.DataFrame({"value": [self.value]})


class FakeAgent:
    def __init__(self, answer=None, error=None):
        self.answer, self.error = answer, error
        self.threads = []

    def ask(self, question, *, thread_id):
        self.threads.append(thread_id)
        if self.error:
            raise self.error
        return TurnResult(self.answer, [], 1, 10, 5, 0, 0.1)


def run(agent, reference=100.4, questions=(QUESTION,)):
    users = {"ceo": object()}
    return run_eval(
        list(questions),
        users=users,
        warehouse=FakeWarehouse(reference),
        make_agent=lambda user: agent,
    )


def test_run_eval_pass_and_fail():
    assert run(FakeAgent("It was $100.")).pop().passed
    assert not run(FakeAgent("It was $90.")).pop().passed


def test_run_eval_flags_a_changed_reference():
    outcome = run(FakeAgent("$150"), reference=150.0).pop()

    assert outcome.passed
    assert outcome.reference_changed


def test_run_eval_uses_a_new_conversation_for_each_question():
    agent = FakeAgent("$100")

    run(agent, questions=(QUESTION, QUESTION))

    assert len(set(agent.threads)) == 2


def test_run_eval_error_fails_only_that_question():
    outcome = run(FakeAgent(error=RuntimeError("model down"))).pop()

    assert not outcome.passed
    assert outcome.error == "model down"
