"""Configuration loaded from environment variables and persisted DB settings."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = Field(default="", alias="BOT_TOKEN")
    admin_ids: str = Field(default="", alias="ADMIN_IDS")
    public_base_url: str = Field(default="https://localhost", alias="PUBLIC_BASE_URL")
    force_sub_channels: str = Field(default="", alias="FORCE_SUB_CHANNELS")
    webhook_secret: str = Field(default="change_me", alias="WEBHOOK_SECRET")
    fernet_key: str = Field(default="", alias="FERNET_KEY")
    host: str = Field(default="0.0.0.0", alias="HOST")
    port: int = Field(default=8000, alias="PORT")
    hosted_port_start: int = Field(default=18000, alias="HOSTED_PORT_START")
    hosted_port_end: int = Field(default=18999, alias="HOSTED_PORT_END")
    data_dir: str = Field(default="./data", alias="DATA_DIR")
    bots_dir: str = Field(default="./bots_storage", alias="BOTS_DIR")
    db_path: str = Field(default="./data/platform.db", alias="DB_PATH")
    default_lang: str = Field(default="ar", alias="DEFAULT_LANG")
    # Public-facing URL for the API documentation site. Defaults to the
    # devinapps.com deployment of the static docs bundle. Override via
    # ``API_DOCS_URL`` when running off a custom domain.
    api_docs_url: str = Field(
        default="https://api-docs-wkuicuhe.devinapps.com/",
        alias="API_DOCS_URL",
    )

    @property
    def admin_id_list(self) -> list[int]:
        return [int(x) for x in self.admin_ids.split(",") if x.strip().isdigit()]

    @property
    def force_sub_list(self) -> list[str]:
        return [c.strip().lstrip("@") for c in self.force_sub_channels.split(",") if c.strip()]

    @property
    def data_path(self) -> Path:
        p = Path(self.data_dir).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def bots_path(self) -> Path:
        p = Path(self.bots_dir).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def db_url(self) -> str:
        p = Path(self.db_path).resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite+aiosqlite:///{p}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
