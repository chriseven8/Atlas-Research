"""A 股主要财务指标（东方财富数据中心）。

**这是数据域，不是角色**：它不新增 agent，也不调用模型，只把已经能取到的主要财务指标
（营收/毛利率/归母净利/加权ROE/经营现金流/资产与负债/利息保障倍数）按报告期整理成
确定性结构，写进证据池。此前报告反复出现「缺少财务报表数据」一类待解问题，而这类数据
本来就能免费取到——问题不是取不到，是没去取。

时点口径：按 **NOTICE_DATE（公告日）** 收口，而不是 REPORT_DATE（报告期末）。
2026 中报的报告期末是 6-30，公告日却是 8-22；按报告期末收口就会把 8 月下旬才公开的
数据拿去解释 6-7 月的行情，这正是本系统最不能出的错。列表按公告日倒序取最近若干期，
因此 `items[0]` 一定是「截止日当时可得的最新一期」。

数值全部取自上游财报接口，不做估算、不做预测、不给目标价。上游字段大多是**报告期累计值**
（如中报是上半年累计，不是单季），这一点在 note 里写明，避免被读成单季。
"""

import math
from datetime import date

import httpx

from .domain import ProviderError
from .providers import evidence

DATA_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
SOURCE_NAME = "东方财富数据中心"
# 业绩报表栏目。具体报表页路径未逐一核验，只指向已核实的栏目根。
SOURCE_URL = "https://data.eastmoney.com/bbsj/"
REPORT_NAME = "RPT_F10_FINANCE_MAINFINADATA"
PAGE_SIZE = 500
# 送进证据与上下文的历史期数。8 期季报约两年，够看出营收/利润的同比方向与毛利率趋势；
# 再往前对「近期经营状况」这个问题没有增量，只会把上下文撑大。
MAX_PERIODS = 8

# 只取数值与日期字段。上游同一张报表有 160+ 列，混合了银行/保险专用列（多为空），
# 全量取回只会让 payload 与快照变大，不增加信息。
FIELDS = (
    "SECUCODE,REPORT_DATE,REPORT_DATE_NAME,REPORT_TYPE,NOTICE_DATE,CURRENCY,"
    "TOTALOPERATEREVE,TOTALOPERATEREVETZ,"
    "MLR,XSMLL,XSJLL,"
    "PARENTNETPROFIT,PARENTNETPROFITTZ,KCFJCXSYJLR,KCFJCXSYJLRTZ,"
    "ROEJQ,ROEKCJQ,"
    "NETCASH_OPERATE_PK,TOTAL_ASSETS_PK,LIABILITY,ZCFZL,"
    "INTEREST_COVERAGE_RATIO,EPSJB,BPS,TOTAL_EQUITY_PK"
)


def _num(row, field: str) -> float | None:
    """取一个数值字段；缺失、非数值或非有限值一律返回 None。

    返回 None 而不是 0：0 会被下游读成「这一项真的等于零」，而实际上游是把没有的
    项目写成 null。单位保持上游原始口径（金额为元），只做四舍五入到两位小数。
    """
    value = row.get(field)
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, 2) if math.isfinite(number) else None


def _ratio_pct(numerator: float | None, denominator: float | None) -> float | None:
    """比率类派生指标。分母为零或缺任一项时返回 None，不用 0 冒充「没有」。"""
    if numerator is None or denominator in (None, 0):
        return None
    return round(numerator / denominator * 100, 2)


def _period(row) -> dict | None:
    """把一行报表整理成一期指标；缺关键字段则丢弃该行。"""
    try:
        report_date = str(row["REPORT_DATE"])[:10]
        notice_date = str(row["NOTICE_DATE"])[:10]
        date.fromisoformat(report_date)
        date.fromisoformat(notice_date)
    except (KeyError, TypeError, ValueError):
        return None
    revenue = _num(row, "TOTALOPERATEREVE")
    net_profit = _num(row, "PARENTNETPROFIT")
    cash = _num(row, "NETCASH_OPERATE_PK")
    return {
        "report_date": report_date,
        "report_name": str(row.get("REPORT_DATE_NAME") or report_date),
        "report_type": str(row.get("REPORT_TYPE") or ""),
        "notice_date": notice_date,
        "currency": str(row.get("CURRENCY") or "CNY"),
        "revenue": revenue,
        "revenue_yoy_pct": _num(row, "TOTALOPERATEREVETZ"),
        "gross_profit": _num(row, "MLR"),
        "gross_margin_pct": _num(row, "XSMLL"),
        "net_margin_pct": _num(row, "XSJLL"),
        "net_profit": net_profit,
        "net_profit_yoy_pct": _num(row, "PARENTNETPROFITTZ"),
        "deducted_net_profit": _num(row, "KCFJCXSYJLR"),
        "deducted_net_profit_yoy_pct": _num(row, "KCFJCXSYJLRTZ"),
        "roe_weighted_pct": _num(row, "ROEJQ"),
        "roe_deducted_pct": _num(row, "ROEKCJQ"),
        "operating_cash_flow": cash,
        # 经营现金流对归母净利的覆盖：程序算出来的比值，不是上游字段。
        "cash_to_profit_pct": _ratio_pct(cash, net_profit),
        "total_assets": _num(row, "TOTAL_ASSETS_PK"),
        "liability": _num(row, "LIABILITY"),
        "total_equity": _num(row, "TOTAL_EQUITY_PK"),
        "debt_ratio_pct": _num(row, "ZCFZL"),
        "interest_coverage": _num(row, "INTEREST_COVERAGE_RATIO"),
        "eps": _num(row, "EPSJB"),
        "bps": _num(row, "BPS"),
    }


def _periods(rows, cutoff: date) -> list[dict]:
    """按公告日收口并倒序排列，取最近 MAX_PERIODS 期。

    cutoff 在本地再过滤一次，不把时点判断托给上游的 filter：一旦上游忽略它，
    报告就会用到当时尚未公告的财报。报告期末也一并收口，防止上游出现
    「公告日早于报告期末」这种自相矛盾的行。
    """
    seen: dict[str, dict] = {}
    for row in rows:
        item = _period(row)
        if not item:
            continue
        if item["notice_date"] > cutoff.isoformat() or item["report_date"] > cutoff.isoformat():
            continue
        # 同一报告期出现多行（如更正公告）时保留公告日最新的那一版。
        previous = seen.get(item["report_date"])
        if previous is None or item["notice_date"] > previous["notice_date"]:
            seen[item["report_date"]] = item
    ordered = sorted(seen.values(), key=lambda item: item["notice_date"], reverse=True)
    return ordered[:MAX_PERIODS]


def _yi(value: float | None) -> str:
    """金额换算成亿元便于阅读；缺失写「未披露」而不是 0。"""
    return "未披露" if value is None else f"{value / 1e8:.2f} 亿元"


def _pct(value: float | None) -> str:
    return "未披露" if value is None else f"{value:.2f}%"


def _note(symbol: str, periods: list[dict], cutoff: date, as_of: date) -> str:
    """口径说明。日期一律写清单**实际**覆盖的范围。

    这行 note 是模型唯一能看到基本面数值的地方（证据池只带元数据，余额以外的逐期明细
    留在证据快照里），因此必须把最新一期的关键数字写全，并说明它们不是单季值。
    """
    latest = periods[0]
    return (
        f"{symbol} 主要财务指标：清单为最近 {len(periods)} 期报告，实际覆盖 "
        f"{periods[-1]['report_name']} 至 {latest['report_name']}（按公告日倒序）。"
        f"最新一期 {latest['report_name']}（报告期末 {latest['report_date']}，公告日 {latest['notice_date']}）："
        f"营业总收入 {_yi(latest['revenue'])}（同比 {_pct(latest['revenue_yoy_pct'])}）、"
        f"毛利率 {_pct(latest['gross_margin_pct'])}、"
        f"归母净利润 {_yi(latest['net_profit'])}（同比 {_pct(latest['net_profit_yoy_pct'])}）、"
        f"加权ROE {_pct(latest['roe_weighted_pct'])}、"
        f"经营现金流 {_yi(latest['operating_cash_flow'])}（对归母净利 {_pct(latest['cash_to_profit_pct'])}）、"
        f"资产负债率 {_pct(latest['debt_ratio_pct'])}、利息保障倍数 "
        f"{latest['interest_coverage'] if latest['interest_coverage'] is not None else '未披露'}。"
        f"全部为**报告期累计值**，不是单季值；金额单位人民币元。"
        f"已按公告日 NOTICE_DATE <= {cutoff} 收口（研究截止 {as_of}），"
        "因此最新一期通常落后研究截止日一到两个季度。"
        "不含分析师一致预期、盈利预测、目标价与估值模型，也不含浮动/固定利率债务拆分。"
    )


def _fetch_rows(client: httpx.Client, symbol: str, cutoff: date) -> list:
    response = client.get(
        DATA_URL,
        params={
            "reportName": REPORT_NAME,
            "columns": FIELDS,
            # 把公告日下推到数据源减少传输量；真正的收口由 _periods 再兜一层。
            "filter": f"(SECUCODE=\"{symbol}\")(NOTICE_DATE<='{cutoff}')",
            "pageSize": PAGE_SIZE,
            "pageNumber": 1,
            "sortColumns": "NOTICE_DATE",
            "sortTypes": "-1",
        },
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("success") is not True:
        raise ProviderError("主要财务指标：数据源返回失败。")
    data = (payload.get("result") or {}).get("data")
    if data is None:
        # result 为 None 表示该代码在报表里没有记录（未上市/代码不对），
        # 与「字段不存在」是两回事，但都只能如实报一无所获。
        return []
    if not isinstance(data, list):
        raise ProviderError("主要财务指标：数据源未返回有效列表。")
    return data


def fetch_china_fundamentals(req, settings, transport=None) -> dict:
    """取 A 股主要财务指标。取不到时抛 ProviderError，由节点层降级并如实标注。"""
    periods, evidence_list = [], []
    with httpx.Client(
        timeout=settings.http_timeout_seconds,
        transport=transport,
        headers={"User-Agent": "AtlasResearch/0.1", "Referer": "https://data.eastmoney.com/"},
    ) as client:
        rows = _fetch_rows(client, req.symbol, req.as_of)
        periods = _periods(rows, req.as_of)
    if not periods:
        raise ProviderError(f"主要财务指标：{req.symbol} 在 {req.as_of} 前无可用报告期记录；未以估计值替代。")
    latest = periods[0]
    evidence_list.append(
        evidence(
            "fundamental",
            f"{req.symbol} 主要财务指标（{latest['report_name']}，公告 {latest['notice_date']}）",
            SOURCE_NAME,
            latest["notice_date"],
            periods,
            False,
            url=SOURCE_URL,
            note=_note(req.symbol, periods, req.as_of, req.as_of),
        )
    )
    return {
        "items": periods,
        "latest": latest,
        "evidence": evidence_list,
        "warnings": [
            "财务指标为报告期累计值（如中报为上半年累计），且为公告口径的修订值；"
            "按公告日收口，因此最新一期通常落后研究截止日一到两个季度。",
            "仅含主要财务指标：不含分部收入、有息负债期限结构、浮动/固定利率拆分、分析师一致预期与估值模型。",
        ],
    }
