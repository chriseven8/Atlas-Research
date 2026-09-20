from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_source_root = Path(__file__).resolve().parents[3]
ROOT = _source_root if (_source_root / "backend" / "pyproject.toml").is_file() else Path.cwd()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore", env_ignore_empty=True)

    database_url: str = "sqlite:///" + (ROOT / "data" / "research.db").as_posix()
    embed_worker: bool = True
    alpha_vantage_api_key: str = ""
    openai_api_key: str = ""
    openai_model: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    http_timeout_seconds: float = Field(default=25, ge=1, le=90)
    task_timeout_seconds: int = Field(default=240, ge=30, le=1800)
    max_llm_output_tokens: int = Field(default=2400, ge=256, le=8000)
    max_llm_calls: int = Field(default=1, ge=1, le=3)
    llm_input_price_per_million: float | None = Field(default=None, ge=0)
    llm_output_price_per_million: float | None = Field(default=None, ge=0)
    lease_seconds: int = Field(default=60, ge=10)
    max_task_attempts: int = Field(default=3, ge=1, le=5)

    @field_validator("openai_base_url")
    @classmethod
    def secure_base_url(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("OPENAI_BASE_URL 必须使用 HTTPS")
        return value.rstrip("/")

    @property
    def live_ready(self) -> bool:
        return True  # Public daily market data does not require an API key.

    @property
    def llm_ready(self) -> bool:
        return bool(self.openai_api_key and self.openai_model)
