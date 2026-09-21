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
    # 这四项是一组：推理模型（如 deepseek-v4-flash）的单次输出里 reasoning_tokens 与正文
    # 共享 max_output_tokens，按非推理模型的直觉给预算会让输出被截断；而放宽单次超时又会
    # 拉长整条链路的墙钟时间。改其中任何一个都要连带检查其余三个。
    http_timeout_seconds: float = Field(default=60, ge=1, le=90)
    task_timeout_seconds: int = Field(default=600, ge=30, le=1800)
    # 上限按实测定：risk（要读完其余角色的全部结论再审查）在 8000 上仍会被 API 判
    # incomplete/max_output_tokens，16000 才有余量；le 留到 32000 供更重的模型自行上调。
    max_llm_output_tokens: int = Field(default=16000, ge=256, le=32000)
    # 一次完整研究的模型调用上限：初轮 8 个角色 + 一轮返工 4 个（technical/news/macro/risk@1）
    # = 12 次是实际最坏值，留 4 次余量给降级与重试路径，不至于刚好用满就饿死后面的角色。
    max_llm_calls: int = Field(default=16, ge=1, le=16)
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
