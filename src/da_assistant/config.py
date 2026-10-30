"""Settings and the list of users.

Settings come from environment variables or a `.env` file (see `.env.example`).
The users and the brands each of them can analyze come from `config/users.yaml`.
"""

from pathlib import Path
from typing import Self

import yaml
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(Exception):
    """The configuration is missing or wrong. The message says what to fix."""


class Settings(BaseSettings):
    """App settings. Every field can be set with an environment variable."""

    model_config = SettingsConfigDict(
        env_prefix="DA_",
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
    )

    gemini_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    )
    # The project that pays for BigQuery jobs. None means the gcloud default project.
    gcp_project: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DA_GCP_PROJECT", "GOOGLE_CLOUD_PROJECT"),
    )
    dataset: str = "bigquery-public-data.thelook_ecommerce"

    llm_model: str = "gemini-3.8-flash"
    llm_fallback_model: str = "gemini-3.5-flash-lite"
    llm_timeout_s: float = 60.0
    llm_attempts: int = 3

    # Hard limits for every BigQuery job. BigQuery itself stops a job that goes over them.
    max_bytes_billed: int = 1_000_000_000
    bq_timeout_s: float = 60.0

    # Limits for one chat turn (one question and its answer).
    max_llm_calls_per_turn: int = 8
    max_sql_retries: int = 2
    max_bytes_per_turn: int = 5_000_000_000
    # Rows kept from a query result, and rows of it shown to the model.
    max_result_rows: int = 1000
    rows_for_model: int = 50

    users_file: Path = Path("config/users.yaml")


def load_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        raise ConfigError(f"Invalid settings:\n{exc}") from exc


class UserProfile(BaseModel):
    """A person who can use the assistant, and the brands they can analyze."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    name: str
    brands: tuple[str, ...] = ()
    all_brands: bool = False

    @model_validator(mode="after")
    def _check_scope(self) -> Self:
        if self.all_brands == bool(self.brands):
            raise ValueError("set exactly one of: a list of 'brands', or 'all_brands: true'")
        return self

    @property
    def brand_scope(self) -> tuple[str, ...] | None:
        """The brands this user can analyze. None means all brands."""
        return None if self.all_brands else self.brands

    def scope_text(self) -> str:
        return "all brands" if self.all_brands else ", ".join(self.brands)


def load_users(path: Path) -> dict[str, UserProfile]:
    """Read the users file. Returns the users by id."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Users file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"Users file {path} is not valid YAML: {exc}") from exc

    entries = data.get("users") if isinstance(data, dict) else None
    if not isinstance(entries, dict) or not entries:
        raise ConfigError(f"Users file {path} must have a non-empty 'users' mapping.")

    users = {}
    for user_id, fields in entries.items():
        try:
            users[str(user_id)] = UserProfile(id=str(user_id), **(fields or {}))
        except (TypeError, ValidationError) as exc:
            raise ConfigError(f"User '{user_id}' in {path} is invalid: {exc}") from exc
    return users
