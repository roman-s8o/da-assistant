"""Traces: what each turn did, step by step, as JSON lines in `data/traces.jsonl`.

Each step of a turn (guard, model call, tool call, confirmation, final check) is one
"span" line; the end of the turn is one "turn" line with its totals.

What a trace keeps: ids, names, counts, sizes, timings, tokens, bytes, error codes, and
the SQL as the model wrote it, with anything that looks like personal data masked. What it
never keeps: the text of questions, answers and reports. A question or an answer is stored as a
fingerprint, a short SHA-256 and the length. It shows that two turns had the same
answer, or checks a text someone shows you, but it does not hold the text. So a deleted
report cannot live on in the traces, and the traces need no deletion of their own.
"""

import hashlib
import json
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langgraph.errors import GraphBubbleUp

from da_assistant.errors import short_message
from da_assistant.privacy import mask_text

# How much of a SQL query or a model-written purpose a trace keeps.
MAX_SQL_CHARS = 2000
MAX_NOTE_CHARS = 200


def fingerprint(text: str) -> dict[str, Any]:
    """A short hash and the length of a text, instead of the text."""
    return {"sha256": hashlib.sha256(text.encode()).hexdigest()[:16], "chars": len(text)}


def safe_text(text: str, limit: int = MAX_NOTE_CHARS) -> str:
    """Text written by the model or BigQuery (SQL, a purpose, an error), masked and cut."""
    return mask_text(text)[0][:limit]


def error_fields(exc: BaseException) -> dict[str, str]:
    return {"error_type": type(exc).__name__, "error": safe_text(short_message(exc))}


class Tracer:
    """Writes the traces of one user. `path=None` keeps them in memory only."""

    def __init__(self, path: Path | None, *, user_id: str) -> None:
        self.path = path
        self.user_id = user_id
        # The turn records of this session, for /stats.
        self.turns: list[dict[str, Any]] = []
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def new_turn(self, conversation_id: str) -> "TurnTrace":
        return TurnTrace(self, conversation_id)

    def write(self, record: dict[str, Any]) -> None:
        if self.path is not None:
            with self.path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(record, default=str) + "\n")


class TurnTrace:
    def __init__(self, tracer: Tracer, conversation_id: str) -> None:
        self.tracer = tracer
        self.trace_id = uuid.uuid4().hex[:16]
        self.conversation_id = conversation_id
        self.started_at = datetime.now(UTC)
        self.spans: list[dict[str, Any]] = []
        # Time spent working, without the time the user takes to answer a confirmation.
        self.busy_s = 0.0
        # The turn record, once the turn is finished.
        self.record: dict[str, Any] = {}

    @contextmanager
    def span(self, name: str, **fields: Any) -> Iterator[dict[str, Any]]:
        """Time a step. The caller adds fields to the dict it gets."""
        record: dict[str, Any] = {"type": "span", "trace_id": self.trace_id, "span": name}
        record.update(fields)
        start = time.perf_counter()
        try:
            yield record
        except GraphBubbleUp:
            # The graph paused for a confirmation: not an error, and the step runs again
            # on resume, so nothing is recorded now. The pause must go on up.
            raise
        except Exception as exc:
            record.update(status="error", **error_fields(exc))
            self._close(record, start)
            raise
        record.setdefault("status", "ok")
        self._close(record, start)

    def _close(self, record: dict[str, Any], start: float) -> None:
        record["ms"] = round((time.perf_counter() - start) * 1000)
        self.spans.append(record)
        self.tracer.write(record)

    def finish(self, **fields: Any) -> dict[str, Any]:
        record = {
            "type": "turn",
            "trace_id": self.trace_id,
            "user": self.tracer.user_id,
            "conversation": self.conversation_id,
            "started_at": self.started_at.isoformat(),
            "latency_ms": round(self.busy_s * 1000),
            **fields,
        }
        self.record = record
        self.tracer.turns.append(record)
        self.tracer.write(record)
        return record


def read_turns(path: Path) -> list[dict[str, Any]]:
    """The turn records of a trace file."""
    if not path.exists():
        return []
    turns = []
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record.get("type") == "turn":
            turns.append(record)
    return turns
