import re
from datetime import UTC, date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


class ResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(default="AAPL", min_length=1, max_length=12)
    market: Literal["US", "CN"] = "US"
    question: str = Field(default="分析近期价格趋势、新闻催化因素与主要风险。", min_length=3, max_length=1200)
    mode: Literal["demo", "live"] = "demo"
    as_of: date = Field(default_factory=date.today)
    lookback_days: int = Field(default=90, ge=30, le=100)
    use_llm: bool = False

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value):
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("question")
    @classmethod
    def clean_question(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 3:
            raise ValueError("研究问题至少需要三个字符")
        return value

    @model_validator(mode="after")
    def validate_scope(self):
        if self.market == "CN":
            match = re.fullmatch(r"(?:(SH|SZ|BJ))?(\d{6})(?:\.(SH|SZ|BJ))?", self.symbol)
            if not match:
                raise ValueError("A 股请输入六位代码，例如 600519、000001 或 600519.SH")
            prefix, code, suffix = match.groups()
            exchange = (
                "SH"
                if code.startswith(("600", "601", "603", "605", "688", "689"))
                else "SZ"
                if code.startswith(("000", "001", "002", "003", "300", "301"))
                else "BJ"
                if code.startswith(("4", "8", "920"))
                else None
            )
            if not exchange or any(x and x != exchange for x in (prefix, suffix)):
                raise ValueError("A 股代码与交易所不匹配或不在支持范围")
            self.symbol = f"{code}.{exchange}"
        elif not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,11}", self.symbol):
            raise ValueError("美股请输入股票代码，例如 AAPL、MSFT")
        if self.as_of > date.today():
            raise ValueError("截止日期不能晚于今天")
        if self.as_of < date(2000, 1, 1):
            raise ValueError("截止日期不能早于 2000-01-01")
        if self.mode == "demo" and (
            self.market != "US" or self.symbol not in {"AAPL", "MSFT", "NVDA", "SPY"}
        ):
            raise ValueError("演示模式支持 AAPL、MSFT、NVDA、SPY")
        return self


class Bar(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    date: date
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: int = Field(ge=0)

    @model_validator(mode="after")
    def valid_ohlc(self):
        if self.high < max(self.open, self.close, self.low) or self.low > min(self.open, self.close):
            raise ValueError("OHLC 数据不一致")
        return self


class Evidence(BaseModel):
    id: str
    title: str
    source: str
    url: str | None = None
    observed_at: str
    retrieved_at: str = Field(default_factory=utcnow)
    kind: Literal["market", "news", "macro"]
    is_demo: bool
    snapshot_hash: str
    note: str = ""


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    evidence_ids: list[str]
    kind: Literal["fact", "interpretation"]


class Synthesis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str
    claims: list[Claim]
    uncertainties: list[str]


class FocusNote(BaseModel):
    """某个角色的一项说明：本轮关注点，或未启用该角色的原因。"""

    model_config = ConfigDict(extra="forbid")
    agent: str
    note: str


class ResearchPlan(BaseModel):
    """Manager 的 LLM 输出契约，决定本轮启用哪些可选角色。

    strict 模式要求所有属性都进 required、且不允许 map 类型，因此 focus / skipped_reason
    用列表而非 dict；normalize_plan 负责转成按角色索引的内部形状供报告与前端使用。
    """

    model_config = ConfigDict(extra="forbid")
    enabled_agents: list[str]
    rationale: str
    focus: list[FocusNote]
    skipped_reason: list[FocusNote]


class AgentFinding(BaseModel):
    """所有研究型 agent 的统一输出契约。

    集合类字段不给默认值：strict 模式下带默认值的属性无法进 required，模型必须显式返回空数组。
    """

    model_config = ConfigDict(extra="forbid")
    headline: str
    findings: list[Claim]
    confidence: Literal["high", "medium", "low"]
    open_questions: list[str]


class Challenge(BaseModel):
    """风控角色对其他 agent 提出的返工要求。"""

    model_config = ConfigDict(extra="forbid")
    target_agent: str
    reason: str
    request: str


class RiskReview(AgentFinding):
    """风控输出：在通用 finding 之上附加可执行的返工要求。"""

    challenges: list[Challenge]


class Arbitration(BaseModel):
    """仲裁角色对冲突结论的裁决。"""

    model_config = ConfigDict(extra="forbid")
    conflict: str
    ruling: str
    rationale: str
    evidence_ids: list[str]


class ProviderError(RuntimeError):
    """Public-safe provider error: never contains credentials or request URLs."""


class LeaseLost(RuntimeError):
    pass


class OutputTruncated(ProviderError):
    """模型输出被 max_output_tokens 截断，需要与请求失败区分开。"""
