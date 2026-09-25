import json

import httpx

from .agents import AgentSpec
from .domain import AgentFinding, Arbitration, OutputTruncated, ProviderError, ResearchPlan, Synthesis
from .settings import Settings

# 一份 finding 允许的最大条数。超限即整份作废（不是截断），所以这是「别再往上加」的告警线。
MAX_CLAIMS = 20


def _extract_text(data: dict) -> str:
    """从 Responses 输出中取出正文，跳过推理模型产生的 reasoning 条目。"""
    return "".join(
        c.get("text", "")
        for m in data.get("output", [])
        if m.get("type") == "message"
        for c in m.get("content", [])
        if c.get("type") == "output_text"
    )


def validate_citations(result, allowed: set[str]) -> None:
    """引用校验。只证明引用 ID 存在于本次提供的证据集合，不证明推断成立。

    四种输出契约的校验规则不同：
    - ResearchPlan 不产出结论，只要求给出非空规划理由。
    - Arbitration 的裁决必须以非空 ruling 表述，其 evidence_ids 必须是提供集合的子集。
    - AgentFinding / RiskReview 与 Synthesis 都带 Claim 列表，要求结论非空且每条都引用了合法证据 ID。

    按契约显式分派而非鸭子类型：新增契约若没在这里接上规则，必须直接拒绝，
    否则会被下一条 `result.claims` 之类的猜测式取属性静默漏检。
    """
    if isinstance(result, ResearchPlan):
        if not result.rationale.strip():
            raise ValueError("empty rationale")
        return
    if isinstance(result, Arbitration):
        if not result.ruling.strip():
            raise ValueError("empty ruling")
        if not set(result.evidence_ids) <= allowed:
            raise ValueError("invalid citation")
        return
    if isinstance(result, (AgentFinding, Synthesis)):
        claims = result.findings if isinstance(result, AgentFinding) else result.claims
        headline = result.headline if isinstance(result, AgentFinding) else result.summary
        # 上限是「防止一份输出塞爆报告」，不是内容质量门槛：超限即整份作废，
        # 所以它必须留出余量——证据域变多后 300 条日线 + 7 项宏观 + 财务指标
        # 足以让一个角色合理地写出十几条 finding，卡在 12 会静默吃掉整个角色。
        if not claims or len(claims) > MAX_CLAIMS or not headline.strip():
            raise ValueError("empty or oversized findings")
        if any(
            not c.text.strip() or not c.evidence_ids or not set(c.evidence_ids) <= allowed for c in claims
        ):
            raise ValueError("invalid citation")
        return
    raise ValueError("unknown contract")


def _usage(settings: Settings, data: dict, prompt_version: str) -> dict:
    # 用量是记账，不是结论。缺失或为 null 时按 0 计，不能因此让一份合法的 finding 作废。
    raw = data.get("usage") or {}
    tokens_in = int(raw.get("input_tokens", 0))
    tokens_out = int(raw.get("output_tokens", 0))
    cost = None
    if settings.llm_input_price_per_million is not None and settings.llm_output_price_per_million is not None:
        cost = round(
            (
                tokens_in * settings.llm_input_price_per_million
                + tokens_out * settings.llm_output_price_per_million
            )
            / 1_000_000,
            6,
        )
    return {
        "model": settings.openai_model,
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
        "estimated_cost_usd": cost,
        "prompt_version": prompt_version,
    }


def call_agent(settings: Settings, spec: AgentSpec, context: dict, transport=None) -> tuple[dict, dict]:
    """按 AgentSpec 的角色设定与输出契约调用模型，返回 (结构化结果, 用量)。"""
    if not settings.llm_ready:
        raise ProviderError("AI 研判未配置：需要 OPENAI_API_KEY 和 OPENAI_MODEL。")
    payload = {
        "model": settings.openai_model,
        "store": False,
        "input": [
            {"role": "system", "content": spec.system_prompt()},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
        ],
        "max_output_tokens": settings.max_llm_output_tokens,
        "text": {
            "format": {
                "type": "json_schema",
                "name": spec.schema.__name__,
                "strict": True,
                "schema": spec.schema.model_json_schema(),
            }
        },
    }
    try:
        with httpx.Client(timeout=settings.http_timeout_seconds, transport=transport) as client:
            response = client.post(
                f"{settings.openai_base_url}/responses",
                json=payload,
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            )
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError):
        raise ProviderError("模型请求失败；已保留确定性研究结果。请检查网络、模型名称与 API 权限。") from None
    try:
        if data.get("status") != "completed":
            reason = (data.get("incomplete_details") or {}).get("reason")
            if reason == "max_output_tokens":
                raise OutputTruncated(
                    "模型输出预算不足被截断；已保留确定性研究结果。请调高 MAX_LLM_OUTPUT_TOKENS。"
                )
            raise ValueError("incomplete")
        text = _extract_text(data)
        result = spec.schema.model_validate_json(text)
        validate_citations(result, {e["id"] for e in context.get("evidence", [])})
    except OutputTruncated:
        # 显式放行：截断是预算问题，不能被下面的宽 except 归成「输出不合法」。
        # 这行也挡住日后有人往下面的元组里加 RuntimeError 而误吞截断信号。
        raise
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ProviderError("模型输出未通过结构或引用校验，未将其纳入报告；已保留确定性研究结果。") from None
    # 用量解析放在校验之外：它出错不该丢弃一份已通过校验的结论，也不该伪装成校验失败。
    return result.model_dump(), _usage(settings, data, spec.prompt_version)
