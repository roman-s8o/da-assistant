"""Retries and the fallback model for every Gemini call.

Measured on 2026-10-30, during an outage of the primary model: a single attempt failed
after 21 to 58 seconds (one HTTP request each; how it was measured is in
docs/ARCHITECTURE.md). So retrying an overloaded model mostly makes the user wait. The policy:

- Temporary errors (408, 429, 5xx, timeouts, lost connections) are retried with
  exponential backoff and jitter, but only while less than `retry_window_s` has passed
  since the first attempt. A quick failure, such as a 429, gets a retry. A slow failure
  goes straight to the fallback model.
- "Model not found" (404) and a used-up quota per day (a 429 that says so) are not
  retried: the fallback model takes over. A quota per day comes back only at midnight
  Pacific time, and each model has its own (on 2026-11-02 the free tier allowed 500
  requests a day for gemini-3.5-flash-lite and 20 for gemini-3.8-flash).
- Other errors (400, 401, 403) are raised: another model would fail the same way.
- After the primary model fails, it is skipped for `cooldown_s`, so the next calls do
  not wait for it again (a simple circuit breaker). Then it gets another chance.
- If the fallback model fails while the primary rests, the primary gets one more attempt,
  with no retries. It may have recovered, or it may only be slow: during development the
  fallback returned 503 for minutes while the primary still answered.

The fallback model accepts the history as it is, including tool calls that the primary
model made earlier in the turn (checked on 2026-10-30).
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from tenacity import (
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    stop_after_delay,
    wait_exponential_jitter,
)

from da_assistant.errors import daily_quota_reached

TEMPORARY_CODES = frozenset({408, 429, 500, 502, 503, 504})
# The error label of a used-up quota per day, in the traces.
DAILY_QUOTA_LABEL = "429 per day"
_NETWORK_ERRORS = (httpx.TimeoutException, httpx.NetworkError, TimeoutError, ConnectionError)


def error_code(exc: BaseException) -> int | None:
    """The HTTP status code of an API error, also when a library wrapped it."""
    current: BaseException | None = exc
    while current is not None:
        code = getattr(current, "code", None)
        if isinstance(code, int):
            return code
        current = current.__cause__
    return None


def is_temporary(exc: BaseException) -> bool:
    if daily_quota_reached(exc):
        return False
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, _NETWORK_ERRORS):
            return True
        current = current.__cause__
    return error_code(exc) in TEMPORARY_CODES


def worth_fallback(exc: BaseException) -> bool:
    return is_temporary(exc) or error_code(exc) == 404 or daily_quota_reached(exc)


def error_label(exc: BaseException) -> str:
    """A short label for traces, for example "503", "429 per day" or "timeout"."""
    if daily_quota_reached(exc):
        return DAILY_QUOTA_LABEL
    code = error_code(exc)
    if code is not None:
        return str(code)
    return "timeout" if is_temporary(exc) else type(exc).__name__


@dataclass
class CallInfo:
    """What happened in one model call, for the traces."""

    model: str
    attempts: int
    fallback: bool
    errors: list[str]
    seconds: float
    # The primary model was not tried: it is resting after a failure.
    skipped_primary: bool = False
    # The primary model was resting, the fallback failed, so the primary got one more attempt.
    primary_retried: bool = False


@dataclass
class _Health:
    """Shared by a model and the copies made from it (with tools, with a schema)."""

    primary_down_until: float = 0.0
    last_call: CallInfo | None = None


class ResilientChat:
    """A chat model with retries and a fallback model. Use it like the model itself."""

    def __init__(
        self,
        primary: Any,
        fallback: Any | None = None,
        *,
        primary_name: str,
        fallback_name: str | None = None,
        attempts: int = 3,
        retry_window_s: float = 10.0,
        cooldown_s: float = 300.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        health: _Health | None = None,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.primary_name = primary_name
        self.fallback_name = fallback_name
        self.attempts = attempts
        self.retry_window_s = retry_window_s
        self.cooldown_s = cooldown_s
        self.sleep = sleep
        self.clock = clock
        self.health = health or _Health()

    @property
    def last_call(self) -> CallInfo | None:
        return self.health.last_call

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ResilientChat":
        return self._derive(lambda model: model.bind_tools(tools, **kwargs))

    def with_structured_output(self, schema: Any, **kwargs: Any) -> "ResilientChat":
        return self._derive(lambda model: model.with_structured_output(schema, **kwargs))

    def invoke(self, messages: Any) -> Any:
        start = self.clock()
        errors: list[str] = []
        attempts = [0]
        resting = self.fallback is not None and self.clock() < self.health.primary_down_until
        if not resting:
            try:
                result = self._call(self.primary, messages, errors, attempts)
                self._record(self.primary_name, False, attempts, errors, start)
                return result
            except Exception as exc:
                if self.fallback is None or not worth_fallback(exc):
                    self._record(self.primary_name, False, attempts, errors, start)
                    raise
                self.health.primary_down_until = self.clock() + self.cooldown_s
        try:
            result = self._call(self.fallback, messages, errors, attempts)
        except Exception as exc:
            if not (resting and worth_fallback(exc)):
                self._record(self.fallback_name, True, attempts, errors, start, skipped=resting)
                raise
            return self._retry_resting_primary(messages, errors, attempts, start)
        self._record(self.fallback_name, True, attempts, errors, start, skipped=resting)
        return result

    def _retry_resting_primary(
        self, messages: Any, errors: list[str], attempts: list[int], start: float
    ) -> Any:
        """The fallback failed while the primary rests: one more attempt of the primary."""
        try:
            result = self._call(self.primary, messages, errors, attempts, max_attempts=1)
        except Exception:
            self.health.primary_down_until = self.clock() + self.cooldown_s
            self._record(self.primary_name, False, attempts, errors, start, retried=True)
            raise
        self.health.primary_down_until = 0.0  # it answered, so it rests no more
        self._record(self.primary_name, False, attempts, errors, start, retried=True)
        return result

    def _call(
        self,
        model: Any,
        messages: Any,
        errors: list[str],
        attempts: list[int],
        max_attempts: int | None = None,
    ) -> Any:
        def attempt() -> Any:
            attempts[0] += 1
            try:
                return model.invoke(messages)
            except Exception as exc:
                errors.append(error_label(exc))
                raise

        retrying = Retrying(
            stop=stop_after_attempt(max_attempts or self.attempts)
            | stop_after_delay(self.retry_window_s),
            wait=wait_exponential_jitter(initial=1, max=4),
            retry=retry_if_exception(is_temporary),
            sleep=self.sleep,
            reraise=True,
        )
        return retrying(attempt)

    def _record(
        self, name, fallback, attempts, errors, start, skipped=False, retried=False
    ) -> None:
        self.health.last_call = CallInfo(
            str(name),
            attempts[0],
            fallback,
            list(errors),
            self.clock() - start,
            skipped_primary=skipped,
            primary_retried=retried,
        )

    def _derive(self, change: Callable[[Any], Any]) -> "ResilientChat":
        return ResilientChat(
            change(self.primary),
            change(self.fallback) if self.fallback is not None else None,
            primary_name=self.primary_name,
            fallback_name=self.fallback_name,
            attempts=self.attempts,
            retry_window_s=self.retry_window_s,
            cooldown_s=self.cooldown_s,
            sleep=self.sleep,
            clock=self.clock,
            health=self.health,
        )
