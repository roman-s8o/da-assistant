from typer.testing import CliRunner

from da_assistant import cli
from da_assistant.health import CheckResult

runner = CliRunner()


def test_chat_rejects_unknown_user():
    result = runner.invoke(cli.app, ["chat", "--user", "mallory"])

    assert result.exit_code == 2
    assert "Unknown user 'mallory'" in result.output
    assert "alice" in result.output


def test_chat_session_starts_and_ends():
    result = runner.invoke(cli.app, ["chat", "--user", "ceo"], input="/help\n\nhello\n/exit\n")

    assert result.exit_code == 0
    assert "You can analyze: all brands" in result.output
    assert "/exit" in result.output
    assert "not connected yet" in result.output
    assert "Bye." in result.output


def test_chat_ends_on_end_of_input():
    result = runner.invoke(cli.app, ["chat", "--user", "bob"], input="")

    assert result.exit_code == 0
    assert "Bye." in result.output


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
