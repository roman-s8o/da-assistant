from pathlib import Path

import pytest
import yaml

from da_assistant.config import ConfigError, load_settings, load_users


def write_users(folder: Path, users: object) -> Path:
    path = folder / "users.yaml"
    path.write_text(yaml.safe_dump({"users": users}), encoding="utf-8")
    return path


def test_shipped_users_file_is_valid(users_file):
    users = load_users(users_file)

    assert users["ceo"].all_brands
    assert users["alice"].brands
    assert not users["alice"].all_brands


def test_scope_text(users_file):
    users = load_users(users_file)

    assert users["ceo"].scope_text() == "all brands"
    assert users["bob"].scope_text() == ", ".join(users["bob"].brands)


@pytest.mark.parametrize(
    "fields",
    [
        {"name": "No scope"},
        {"name": "Both scopes", "brands": ["A"], "all_brands": True},
    ],
)
def test_user_needs_exactly_one_scope(tmp_path, fields):
    path = write_users(tmp_path, {"x": fields})

    with pytest.raises(ConfigError, match="exactly one"):
        load_users(path)


def test_unknown_user_field_is_rejected(tmp_path):
    path = write_users(tmp_path, {"x": {"name": "Typo", "brand": ["A"]}})

    with pytest.raises(ConfigError, match="User 'x'"):
        load_users(path)


def test_missing_users_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_users(tmp_path / "nope.yaml")


@pytest.mark.parametrize("text", ["", "users: {}", "users: [a, b]", "{{not yaml"])
def test_bad_users_file(tmp_path, text):
    path = tmp_path / "users.yaml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ConfigError):
        load_users(path)


def test_settings_defaults():
    settings = load_settings()

    assert settings.gemini_api_key is None
    assert settings.gcp_project is None
    assert settings.dataset == "bigquery-public-data.thelook_ecommerce"
    assert settings.max_bytes_billed > 0


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "secret-key")
    monkeypatch.setenv("DA_LLM_MODEL", "some-model")
    monkeypatch.setenv("DA_MAX_BYTES_BILLED", "5")

    settings = load_settings()

    assert settings.gemini_api_key.get_secret_value() == "secret-key"
    assert "secret-key" not in repr(settings)
    assert settings.llm_model == "some-model"
    assert settings.max_bytes_billed == 5


def test_settings_from_dotenv_ignore_empty_values(tmp_path):
    # The tests run in tmp_path, so this is the .env file the settings read.
    (tmp_path / ".env").write_text("GEMINI_API_KEY=\nDA_GCP_PROJECT=my-project\n")

    settings = load_settings()

    assert settings.gemini_api_key is None
    assert settings.gcp_project == "my-project"


def test_bad_setting_is_a_config_error(monkeypatch):
    monkeypatch.setenv("DA_MAX_BYTES_BILLED", "a lot")

    with pytest.raises(ConfigError, match="max_bytes_billed"):
        load_settings()
