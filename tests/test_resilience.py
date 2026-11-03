import time

import httpx
import pytest
from fakes import quota_error
from google.genai import errors as genai_errors

from da_assistant.resilience import (
    DAILY_QUOTA_LABEL,
    ResilientChat,
    error_label,
    is_temporary,
    worth_fallback,
)


def api_error(code: int) -> genai_errors.APIError:
    cls = genai_errors.ServerError if code >= 500 else genai_errors.ClientError
    return cls(code, {"error": {"code": code, "message": "x", "status": "X"}})


def wrapped(error: Exception) -> Exception:
    """Like LangChain: its own error class, with the SDK error as the cause."""
    try:
        raise error
    except Exception as cause:
        try:
            raise RuntimeError("Error calling model") from cause
        except RuntimeError as outer:
            return outer


class FakeModel:
    """Fails with the given errors first, then answers. Can be slow."""

    def __init__(self, name, *errors, delay=0.0):
        self.name = name
        self.errors = list(errors)
        self.delay = delay
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        time.sleep(self.delay)
        if self.errors:
            raise self.errors.pop(0)
        return f"answer from {self.name}"

    def bind_tools(self, tools):
        return self

    def with_structured_output(self, schema, **kwargs):
        return self


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def resilient(primary, fallback=None, clock=None, **options):
    return ResilientChat(
        primary,
        fallback,
        primary_name=primary.name,
        fallback_name=fallback.name if fallback else None,
        sleep=lambda seconds: None,
        clock=clock or time.monotonic,
        **options,
    )


@pytest.mark.parametrize(
    ("error", "temporary", "fallback"),
    [
        (api_error(429), True, True),
        (api_error(503), True, True),
        (wrapped(api_error(504)), True, True),
        (httpx.ReadTimeout("slow"), True, True),
        (api_error(404), False, True),
        (api_error(400), False, False),
        (api_error(403), False, False),
        (ValueError("bug"), False, False),
    ],
)
def test_error_classes(error, temporary, fallback):
    assert is_temporary(error) is temporary
    assert worth_fallback(error) is fallback


def test_error_labels():
    assert error_label(wrapped(api_error(503))) == "503"
    assert error_label(httpx.ConnectTimeout("x")) == "timeout"
    assert error_label(ValueError("x")) == "ValueError"


def test_a_working_primary_answers_alone():
    primary, fallback = FakeModel("big"), FakeModel("small")
    model = resilient(primary, fallback)

    assert model.invoke("q") == "answer from big"
    assert (model.last_call.model, model.last_call.attempts, model.last_call.fallback) == (
        "big",
        1,
        False,
    )
    assert fallback.calls == 0


def test_a_quick_temporary_error_is_retried():
    primary = FakeModel("big", api_error(429))
    model = resilient(primary, FakeModel("small"))

    assert model.invoke("q") == "answer from big"
    assert model.last_call.attempts == 2
    assert model.last_call.errors == ["429"]


def test_a_slow_failure_goes_straight_to_the_fallback():
    # An overloaded model can take a minute to fail: retrying it makes the user wait.
    primary = FakeModel("big", api_error(503), delay=0.06)
    fallback = FakeModel("small")
    model = resilient(primary, fallback, retry_window_s=0.05)

    assert model.invoke("q") == "answer from small"
    assert primary.calls == 1
    assert (model.last_call.model, model.last_call.fallback) == ("small", True)
    assert model.last_call.errors == ["503"]


def test_model_not_found_is_not_retried():
    primary = FakeModel("big", api_error(404))
    model = resilient(primary, FakeModel("small"))

    assert model.invoke("q") == "answer from small"
    assert primary.calls == 1


@pytest.mark.parametrize("code", [400, 401, 403])
def test_errors_that_another_model_would_repeat_are_raised(code):
    fallback = FakeModel("small")
    model = resilient(FakeModel("big", api_error(code)), fallback)

    with pytest.raises(genai_errors.ClientError):
        model.invoke("q")
    assert fallback.calls == 0


def test_after_a_failure_the_primary_rests_for_the_cooldown():
    clock = Clock()
    primary = FakeModel("big", api_error(503))
    model = resilient(primary, FakeModel("small"), clock=clock, attempts=1, cooldown_s=300)
    model.invoke("q")

    model.invoke("q")
    assert primary.calls == 1
    assert model.last_call.skipped_primary
    assert model.last_call.errors == []

    clock.now += 301
    assert model.invoke("q") == "answer from big"
    assert primary.calls == 2


def test_a_failing_fallback_gives_the_resting_primary_one_more_attempt():
    # Seen during development: the fallback returned 503 for minutes, while the resting
    # primary still answered.
    clock = Clock()
    primary, fallback = FakeModel("big", api_error(503)), FakeModel("small")
    model = resilient(primary, fallback, clock=clock, attempts=1, cooldown_s=300)
    model.invoke("q")  # the primary fails and rests, the fallback answers
    fallback.errors.append(api_error(503))

    assert model.invoke("q") == "answer from big"
    call = model.last_call
    assert (call.model, call.fallback, call.primary_retried) == ("big", False, True)
    assert (call.attempts, call.errors) == (2, ["503"])

    # It answered, so it rests no more: the next call goes to it first.
    assert model.invoke("q") == "answer from big"
    assert (primary.calls, fallback.calls) == (3, 2)


def test_the_resting_primary_gets_one_attempt_then_rests_again():
    clock = Clock()
    primary = FakeModel("big", *[api_error(503)] * 3)
    fallback = FakeModel("small")
    model = resilient(primary, fallback, clock=clock, attempts=3, cooldown_s=300)
    model.invoke("q")  # three quick attempts of the primary, then the fallback answers
    assert primary.calls == 3

    clock.now += 200
    fallback.errors.extend([api_error(503)] * 3)
    primary.errors.extend([api_error(503)] * 2)
    with pytest.raises(genai_errors.ServerError):
        model.invoke("q")
    assert primary.calls == 4  # one attempt, no retries
    assert model.last_call.primary_retried
    assert model.last_call.errors == ["503"] * 4

    # Its rest starts again from this failure: still resting 200 seconds later.
    clock.now += 200
    assert model.invoke("q") == "answer from small"
    assert primary.calls == 4


def test_a_fallback_error_another_model_would_repeat_is_raised_at_once():
    clock = Clock()
    primary = FakeModel("big", api_error(503))
    fallback = FakeModel("small")
    model = resilient(primary, fallback, clock=clock, attempts=1)
    model.invoke("q")
    fallback.errors.append(api_error(400))

    with pytest.raises(genai_errors.ClientError):
        model.invoke("q")
    assert primary.calls == 1
    assert not model.last_call.primary_retried


def test_copies_with_tools_share_the_cooldown():
    clock = Clock()
    primary = FakeModel("big", api_error(503))
    plain = resilient(primary, FakeModel("small"), clock=clock, attempts=1)
    with_tools = plain.bind_tools(["a tool"])

    with_tools.invoke("q")
    plain.invoke("q")

    assert primary.calls == 1
    assert plain.last_call is with_tools.last_call


def test_when_both_models_fail_the_error_is_raised():
    model = resilient(
        FakeModel("big", api_error(503)), FakeModel("small", api_error(503)), attempts=1
    )

    with pytest.raises(genai_errors.ServerError):
        model.invoke("q")
    assert model.last_call.fallback
    assert model.last_call.errors == ["503", "503"]


def test_without_a_fallback_the_last_error_is_raised_after_the_retries():
    primary = FakeModel("big", api_error(503), api_error(503), api_error(503))
    model = resilient(primary, attempts=3)

    with pytest.raises(genai_errors.ServerError):
        model.invoke("q")
    assert primary.calls == 3


def test_a_used_up_daily_quota_goes_straight_to_the_fallback():
    primary = FakeModel("flash-lite", wrapped(quota_error("Day")))
    fallback = FakeModel("flash")
    model = resilient(primary, fallback)

    assert model.invoke("hi") == "answer from flash"
    assert primary.calls == 1  # no retry: it comes back only at midnight Pacific time
    assert model.last_call.errors == [DAILY_QUOTA_LABEL]
    assert model.last_call.fallback


def test_a_daily_quota_is_not_temporary_but_worth_the_fallback():
    daily, per_minute = quota_error("Day"), quota_error("Minute")

    assert (is_temporary(daily), worth_fallback(daily)) == (False, True)
    assert (is_temporary(per_minute), worth_fallback(per_minute)) == (True, True)
    assert error_label(daily) == "429 per day"
    assert error_label(per_minute) == "429"
