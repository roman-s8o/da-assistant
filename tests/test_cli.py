import pytest
from typer.testing import CliRunner

from da_assistant import cli
from da_assistant.agent import QueryLog, TurnResult
from da_assistant.config import ConfigError
from da_assistant.health import CheckResult

runner = CliRunner()


def turn(queries=(), llm_calls=2):
    return TurnResult(
        answer="There are **124,452** orders.",
        queries=list(queries),
        llm_calls=llm_calls,
        input_tokens=1200,
        output_tokens=80,
        bytes_scanned=6_200_000,
        seconds=1.5,
    )


def query(rows=1, error=None):
    return QueryLog(purpose="p", sql="SELECT 1", rows=rows, bytes=100, error=error)


class FakeAgent:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.questions = []  # (question, thread_id)

    def ask(self, question, *, thread_id, on_progress=None):
        self.questions.append((question, thread_id))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.fixture
def use_agent(monkeypatch):
    def install(agent):
        monkeypatch.setattr(cli.Agent, "from_settings", lambda settings, user: agent)
        return agent

    return install


def test_chat_rejects_unknown_user():
    result = runner.invoke(cli.app, ["chat", "--user", "mallory"])

    assert result.exit_code == 2
    assert "Unknown user 'mallory'" in result.output
    assert "alice" in result.output


def test_chat_answers_and_shows_the_cost(use_agent):
    agent = use_agent(FakeAgent(turn([query()])))

    result = runner.invoke(cli.app, ["chat", "--user", "ceo"], input="How many orders?\n/exit\n")

    assert result.exit_code == 0
    assert "You can analyze: all brands" in result.output
    assert "There are 124,452 orders." in result.output
    assert "1 query, 6.2 MB scanned" in result.output
    assert agent.questions[0][0] == "How many orders?"
    assert "Bye." in result.output


def test_chat_goes_on_after_an_error(use_agent):
    agent = use_agent(FakeAgent(RuntimeError("Gemini is down"), turn()))

    result = runner.invoke(cli.app, ["chat", "--user", "ceo"], input="q1\nq2\n/exit\n")

    assert result.exit_code == 0
    assert "Sorry, I could not answer this. Gemini is down" in result.output
    assert "There are 124,452 orders." in result.output
    # Both questions belong to one conversation.
    assert len({thread_id for _, thread_id in agent.questions}) == 1


def test_chat_setup_error_is_clear(monkeypatch):
    def missing_key(settings, user):
        raise ConfigError("GEMINI_API_KEY is not set.")

    monkeypatch.setattr(cli.Agent, "from_settings", missing_key)

    result = runner.invoke(cli.app, ["chat", "--user", "ceo"])

    assert result.exit_code == 2
    assert "GEMINI_API_KEY is not set." in result.output


def test_chat_help_and_end_of_input(use_agent):
    agent = use_agent(FakeAgent())

    result = runner.invoke(cli.app, ["chat", "--user", "bob"], input="/help\n\n")

    assert result.exit_code == 0
    assert "/exit" in result.output
    assert agent.questions == []
    assert "Bye." in result.output


def test_turn_summary():
    result = turn([query(), query(rows=0), query(rows=0, error="bad")], llm_calls=1)

    assert cli.turn_summary(result) == (
        "3 queries (2 failed or empty), 6.2 MB scanned · 1 model call, "
        "1,200 tokens in, 80 out · 1.5 s"
    )


def test_check_fails_when_a_check_fails(monkeypatch):
    results = [
        CheckResult("Users file", True, "3 users"),
        CheckResult("BigQuery", False, "[no access]"),
    ]
    monkeypatch.setattr(cli, "run_checks", lambda settings: results)

    result = runner.invoke(cli.app, ["check"])

    assert result.exit_code == 1
    assert "FAIL" in result.output
    # Error text is shown as is, not read as rich markup.
    assert "[no access]" in result.output


def test_check_passes(monkeypatch):
    monkeypatch.setattr(cli, "run_checks", lambda settings: [CheckResult("A", True, "fine")])

    result = runner.invoke(cli.app, ["check"])

    assert result.exit_code == 0
    assert "OK" in result.output


def test_bad_settings_give_a_clear_error(monkeypatch):
    monkeypatch.setenv("DA_MAX_BYTES_BILLED", "a lot")

    result = runner.invoke(cli.app, ["check"])

    assert result.exit_code == 2
    assert "Invalid settings" in result.output
