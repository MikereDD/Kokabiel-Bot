from __future__ import annotations

import importlib.util
import os
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any


class ConfigError(RuntimeError):
    """Raised when required bot configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    tmdb_bearer_token: str
    database_path: Path
    log_path: Path
    allowed_chat_ids: frozenset[int]
    admin_user_ids: frozenset[int]
    language: str = "en-US"
    region: str = "US"
    result_limit: int = 8
    include_adult: bool = False
    log_level: str = "INFO"
    local_bot_api_url: str | None = None
    local_bot_api_file_url: str | None = None


def _load_module(path: Path) -> ModuleType:
    if not path.is_file():
        raise ConfigError(
            f"Configuration file not found: {path}. Create ~/bots/config/kokabielrc.py."
        )
    spec = importlib.util.spec_from_file_location("kokabiel_runtime_config", path)
    if spec is None or spec.loader is None:
        raise ConfigError(f"Unable to load configuration file: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _value(module: ModuleType, name: str, default: Any = None) -> Any:
    env_name = f"KOKABIEL_{name}"
    if env_name in os.environ:
        return os.environ[env_name]
    return getattr(module, name, default)


def _parse_ids(value: Any) -> frozenset[int]:
    if value in (None, "", []):
        return frozenset()
    if isinstance(value, str):
        chunks = [chunk.strip() for chunk in value.split(",") if chunk.strip()]
        return frozenset(int(chunk) for chunk in chunks)
    return frozenset(int(item) for item in value)


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def load_settings() -> Settings:
    default_path = Path.home() / "bots" / "config" / "kokabielrc.py"
    config_path = Path(os.environ.get("KOKABIEL_CONFIG", default_path)).expanduser()
    module = _load_module(config_path)

    bot_token = str(_value(module, "BOT_TOKEN", "")).strip()
    tmdb_token = str(_value(module, "TMDB_BEARER_TOKEN", "")).strip()
    if not bot_token:
        raise ConfigError("BOT_TOKEN is required.")
    if not tmdb_token:
        raise ConfigError("TMDB_BEARER_TOKEN is required.")

    database_path = Path(str(_value(module, "DATABASE_PATH"))).expanduser()
    log_path = Path(str(_value(module, "LOG_PATH"))).expanduser()
    result_limit = int(_value(module, "RESULT_LIMIT", 8))
    if not 1 <= result_limit <= 10:
        raise ConfigError("RESULT_LIMIT must be between 1 and 10.")

    base_url = str(_value(module, "LOCAL_BOT_API_URL", "")).strip() or None
    base_file_url = str(_value(module, "LOCAL_BOT_API_FILE_URL", "")).strip() or None

    return Settings(
        bot_token=bot_token,
        tmdb_bearer_token=tmdb_token,
        database_path=database_path,
        log_path=log_path,
        allowed_chat_ids=_parse_ids(_value(module, "ALLOWED_CHAT_IDS", [])),
        admin_user_ids=_parse_ids(_value(module, "ADMIN_USER_IDS", [])),
        language=str(_value(module, "LANGUAGE", "en-US")),
        region=str(_value(module, "REGION", "US")).upper(),
        result_limit=result_limit,
        include_adult=_parse_bool(_value(module, "INCLUDE_ADULT", False)),
        log_level=str(_value(module, "LOG_LEVEL", "INFO")).upper(),
        local_bot_api_url=base_url,
        local_bot_api_file_url=base_file_url,
    )
