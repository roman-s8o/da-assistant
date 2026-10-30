import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
USERS_FILE = ROOT / "config" / "users.yaml"


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    """Keep tests away from the real .env file, keys and cloud projects."""
    monkeypatch.chdir(tmp_path)
    for name in list(os.environ):
        if name.startswith("DA_") or name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
            monkeypatch.delenv(name)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setenv("DA_USERS_FILE", str(USERS_FILE))


@pytest.fixture
def users_file() -> Path:
    """The users file shipped with the project."""
    return USERS_FILE
