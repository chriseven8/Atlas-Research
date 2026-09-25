"""中国宏观数据（东方财富数据中心）。

两类序列的时点口径不同，必须分开处理：

* **市场观测**（国债收益率）与**公告观测**（存款准备金率）的 REPORT_DATE 就是发布日，
  取到即当时可得，按截止日直接收口即可。
* **统计指标**（CPI / PPI / M2 / PMI）的 REPORT_DATE 是**统计期起点**，实际发布要晚
  一到两个月。若按时统计期收口，就会把 9 月的 CPI 拿去解释 9 月中旬的行情——本系统
  最不能出的错。因此给这类指标加一个保守发布时滞，只放行 as_of 之前已经公布的期次。

原本只取 10 年期国债收益率，模型于是反复追问通胀、信用环境与宏观观测条数；这些数据
本来就能取到，只是没取。现在一次性给全，并修掉从前 note 里「取 start 至 as_of」与实际
清单只覆盖最近 12 个月的口径错位。
"""

import math
from dataclasses import dataclass
from datetime import date, timedelta

import httpx

from .domain import ProviderError
from .providers import evidence

DATA_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
SOURCE_NAME = "东方财富数据中心"
# 经济数据栏目首页。逐指标的详情页路径未逐一核验，故只指向已核实的栏目根。
SOURCE_URL = "https://data.eastmoney.com/cjsj/"
TENOR_SOURCE_URL = "https://data.eastmoney.com/cjsj/zmgzsyl.html"
# 回溯 24 个月：宏观 monthly 只有 12 个点，看不出「月度取样是否掩盖了月内反转」，
# 极值统计需要有更长的窗口才谈得上区间。
SPAN_DAYS = 730
# 送进模型上下文的观测条数上限（每指标）。全窗口的极值由 stats 另给，不受此上限影响。
MAX_ITEMS = 12
# 数据源单页硬上限是 500（请求 1000 也只回 500），国债收益率是日频，
# 24 个月约 530 条，因此必须翻页；MAX_PAGES 兜住异常情况下的无限翻页。
PAGE_SIZE = 500
MAX_PAGES = 4
# 统计指标的保守发布时滞：CPI/PPI/M2 次月 9-15 日发布，从统计期起点算约 40-45 天；
# 制造业 PMI 当月最后一日发布，从月初算约 31 天。取上限，宁可晚一期也不早一期。
STAT_LAG_DAYS = 45
PMI_LAG_DAYS = 31

TENOR_FIELD = "EMM00166466"
TENOR_NAME = "中国 10 年期国债到期收益率"


@dataclass(frozen=True)
class Indicator:
    """一项宏观指标的取数与口径。

    lag_days 只对统计类指标非零；sampling 决定把原始观测压成哪些「看点」：
    monthly 按月取样，changes 只在取值变化时保留一条。
    """

    key: str
    name: str
    report_name: str
    date_field: str
    value_field: str
    unit: str
    lag_days: int = 0
    sampling: str = "monthly"
    url: str | None = None
    source: str = SOURCE_NAME


# CURRENCY_SUPPLY 报表的字段名是反的，别按字面读：
# BASIC_CURRENCY 才是 M2（余额 356 万亿 / 同比 7.5%），CURRENCY 是 M1，FREE_CASH 是 M0。
# 这里取同比而非余额：剪刀差与信用环境的读数看增速，余额量级在对话里没有可比对象。
INDICATORS: tuple[Indicator, ...] = (
    Indicator(
        key="cn_10y",
        name=TENOR_NAME,
        report_name="RPTA_WEB_TREASURYYIELD",
        date_field="SOLAR_DATE",
        value_field=TENOR_FIELD,
        unit="%",
        url=TENOR_SOURCE_URL,
    ),
    Indicator(
        key="cn_cpi",
        name="中国 CPI 同比（全国）",
        report_name="RPT_ECONOMY_CPI",
        date_field="REPORT_DATE",
        value_field="NATIONAL_SAME",
        unit="%",
        lag_days=STAT_LAG_DAYS,
    ),
    Indicator(
        key="cn_ppi",
        name="中国 PPI 同比",
        report_name="RPT_ECONOMY_PPI",
        date_field="REPORT_DATE",
        value_field="BASE_SAME",
        unit="%",
        lag_days=STAT_LAG_DAYS,
    ),
    Indicator(
        key="cn_m2_yoy",
        name="中国 M2 同比",
        report_name="RPT_ECONOMY_CURRENCY_SUPPLY",
        date_field="REPORT_DATE",
        value_field="BASIC_CURRENCY_SAME",
        unit="%",
        lag_days=STAT_LAG_DAYS,
    ),
    Indicator(
        key="cn_m1_yoy",
        name="中国 M1 同比",
        report_name="RPT_ECONOMY_CURRENCY_SUPPLY",
        date_field="REPORT_DATE",
        value_field="CURRENCY_SAME",
        unit="%",
        lag_days=STAT_LAG_DAYS,
    ),
    Indicator(
        key="cn_rrr",
        name="大型金融机构人民币存款准备金率",
        report_name="RPT_ECONOMY_DEPOSIT_RESERVE",
        date_field="REPORT_DATE",
        value_field="INTEREST_RATE_BB",
        unit="%",
        # 该报表的 REPORT_DATE 与 PUBLISH_DATE 实测逐行相同（如 2025-05-07），
        # 即它记的本来就是公告日，不是统计期起点，因此不需要时滞。
        sampling="changes",
    ),
    Indicator(
        key="cn_pmi",
        name="中国制造业 PMI",
        report_name="RPT_ECONOMY_PMI",
        date_field="REPORT_DATE",
        value_field="MAKE_INDEX",
        unit="点",
        lag_days=PMI_LAG_DAYS,
    ),
)


def _observations(
    rows, cutoff: date, value_field=TENOR_FIELD, name=TENOR_NAME, unit="%", date_field="SOLAR_DATE"
):
    """把接口返回的行整理成按日期倒序的观测列表，丢弃无法解析的数值。

    cutoff 在本地再过滤一次，不把截止日的判断完全托给数据源：请求里的 filter
    只是减少传输量，一旦上游忽略它、或应答里混入截止日之后的观测，报告就会用
    未来数据解释历史，而这恰恰是本项目最不能出的错。cutoff 是必填参数，好让
    「忘了按截止日收口」在调用点就暴露，而不是变成一个静默的前视偏差。
    """
    seen = {}
    for row in rows:
        try:
            day = date.fromisoformat(str(row[date_field])[:10])
            value = float(row[value_field])
        except (KeyError, TypeError, ValueError):
            continue
        if day > cutoff or not math.isfinite(value):
            continue
        # 同一天出现多行时保留最后一次，避免重复日期让下游把同一天算两次。
        seen[day] = {"date": day.isoformat(), "value": value, "name": name, "unit": unit}
    return [seen[day] for day in sorted(seen, reverse=True)]


def _monthly_marks(observations):
    """每个月取最后一个可得观测，并保留最近一次观测。

    日频序列直接截前 12 条只会覆盖两三个交易周，「利率环境」读不出方向；
    按月取样等价于 Alpha Vantage 那条月度序列，两边的宏观口径才可比。
    列表本身按日期倒序，因此每月第一次出现的就是当月最后一个观测。
    """
    marks = [observations[0]]
    months = {observations[0]["date"][:7]}
    for item in observations[1:]:
        month = item["date"][:7]
        if month in months:
            continue
        months.add(month)
        marks.append(item)
        if len(marks) >= MAX_ITEMS:
            break
    return marks


def _change_marks(observations):
    """事件型序列只在取值变化时保留一条。

    存款准备金率这类指标常年不动，按月取样会把同一个值铺满 12 行，真正要看的
    「哪几次调整、调到多少」反而被埋掉。列表按日期倒序，与前一条保留项比较即可：
    相同说明是同一次调整的重复观测。
    """
    marks = [observations[0]]
    for item in observations[1:]:
        if item["value"] != marks[-1]["value"]:
            marks.append(item)
        if len(marks) >= MAX_ITEMS:
            break
    return marks


def _marks(observations, sampling: str):
    return _change_marks(observations) if sampling == "changes" else _monthly_marks(observations)


def _stats(observations) -> dict:
    """窗口内**全部**原始观测的条数、首末日期与极值。

    极值必须基于全窗口而非取样后的清单：取样正是「可能掩盖月内反转」的那一步，
    只在取样结果上求极值等于用问题本身去证明问题不存在。
    """
    peak = max(observations, key=lambda item: item["value"])
    trough = min(observations, key=lambda item: item["value"])
    return {
        "n": len(observations),
        "first_date": observations[-1]["date"],
        "last_date": observations[0]["date"],
        "max": {"value": peak["value"], "date": peak["date"]},
        "min": {"value": trough["value"], "date": trough["date"]},
    }


def _note(indicator: Indicator, marks: list[dict], stats: dict, cutoff: date, as_of: date) -> str:
    """口径说明。日期一律写清单**实际**覆盖的范围，不写请求窗口。

    旧版本这里写「取 {start} 至 {as_of} 的观测」，而清单其实只到最近 12 个月，
    模型据此质疑「note 说从 2025-08-19 起、清单却从 2025-10-31 起」——那是文案
    与数据不一致，不是数据缺失。这里的数字全部由 marks / stats 反推。
    """
    parts = [
        f"{indicator.name}：清单为最近 {len(marks)} 条观测，实际覆盖 {marks[-1]['date']} 至 {marks[0]['date']}；",
        f"同期原始观测共 {stats['n']} 条（{stats['first_date']} 至 {stats['last_date']}），"
        f"区间最高 {stats['max']['value']}{indicator.unit}（{stats['max']['date']}）、"
        f"最低 {stats['min']['value']}{indicator.unit}（{stats['min']['date']}）。",
    ]
    if indicator.sampling == "monthly":
        parts.append("按月取样：每月保留当月最后一个可得观测，不覆盖当月内路径；极值取自全窗口原始观测。")
    else:
        parts.append("该序列为事件型，仅保留取值发生变化的观测；未列出的月份表示维持前值。")
    if indicator.lag_days:
        parts.append(
            f"该指标按统计期发布、存在发布时滞，本报告按 {indicator.lag_days} 天保守收口"
            f"（数据截止 {cutoff}，研究截止 {as_of}），以避免用当时尚未公布的数据解释历史；"
            "因此最新一期通常落后研究截止日一到两个月。"
        )
    else:
        parts.append("该序列为市场/公告观测，其日期即当时可得的时点，无需额外时滞。")
    return "".join(parts)


def _fetch_rows(client: httpx.Client, indicator: Indicator, start: date, cutoff: date) -> list:
    """按指标取行，必要时翻页。数据源单页最多 500 条，日频序列会超。"""
    rows: list = []
    page = 1
    while page <= MAX_PAGES:
        params = {
            "reportName": indicator.report_name,
            "columns": f"{indicator.date_field},{indicator.value_field}",
            "pageSize": PAGE_SIZE,
            "pageNumber": page,
            "sortColumns": indicator.date_field,
            "sortTypes": "-1",
            # 把截止日下推到数据源减少传输量；真正的收口由 _observations 再兜一层。
            "filter": f"({indicator.date_field}>='{start}')({indicator.date_field}<='{cutoff}')",
        }
        response = client.get(DATA_URL, params=params)
        response.raise_for_status()
        payload = response.json()
        if payload.get("success") is not True:
            raise ProviderError(f"{indicator.name}：数据源返回失败。")
        result = payload.get("result") or {}
        data = result.get("data")
        if not isinstance(data, list):
            raise ProviderError(f"{indicator.name}：数据源未返回有效列表。")
        rows.extend(data)
        # 以 count 为准判断是否取全：只看「本页是否满页」无法区分「正好取完」与
        # 「还有下一页」，静默丢掉最早的观测正是让 note 写错、极值算错的成因。
        total = result.get("count")
        fetched_all = len(rows) >= total if isinstance(total, int) else len(data) < PAGE_SIZE
        if fetched_all:
            break
        page += 1
    return rows


def fetch_china_macro(req, settings, transport=None):
    start = req.as_of - timedelta(days=SPAN_DAYS)
    indicators, evidence_list, warnings = [], [], []
    all_items: list[dict] = []
    with httpx.Client(
        timeout=settings.http_timeout_seconds,
        transport=transport,
        headers={"User-Agent": "AtlasResearch/0.1", "Referer": "https://data.eastmoney.com/"},
    ) as client:
        for indicator in INDICATORS:
            # 统计类指标的数据截止日要再往前推一个发布时滞，否则请求回来的最新一期
            # 是 as_of 当天还没公布的数——那不是「数据多一点」，是前视偏差。
            cutoff = req.as_of - timedelta(days=indicator.lag_days)
            try:
                rows = _fetch_rows(client, indicator, start, cutoff)
            except (httpx.HTTPError, ValueError, ProviderError):
                # 单项失败只降级这一项：限流往往是随机的，让一个指标拖垮整轮宏观
                # 等于因为 PPI 掉线就说整个利率环境不可用。整体失败在最后统一兜。
                warnings.append(f"{indicator.name}：数据源本轮不可用，该指标缺失；未以估计值替代。")
                continue
            observations = _observations(
                rows, cutoff, indicator.value_field, indicator.name, indicator.unit, indicator.date_field
            )
            if not observations:
                warnings.append(
                    f"{indicator.name}：截止 {req.as_of}（考虑发布时滞后为 {cutoff}）无有效观测，"
                    "该指标缺失；未以估计值替代。"
                )
                continue
            marks = _marks(observations, indicator.sampling)
            stats = _stats(observations)
            ev = evidence(
                "macro",
                indicator.name,
                indicator.source,
                marks[0]["date"],
                marks,
                False,
                url=indicator.url,
                note=_note(indicator, marks, stats, cutoff, req.as_of),
            )
            indicators.append(
                {
                    "key": indicator.key,
                    "name": indicator.name,
                    "unit": indicator.unit,
                    "items": marks,
                    "stats": stats,
                    "evidence_id": ev["id"],
                }
            )
            evidence_list.append(ev)
            all_items.extend(marks)
    if not indicators:
        # 整体失败时逐项原因会被这条异常取代，所以这里必须点名是哪一路数据源，
        # 否则报告的 limitations 只剩一句「无有效观测」，读者无从判断该查什么。
        raise ProviderError(f"中国宏观数据源：截止 {req.as_of} 前无有效宏观观测值；未以估计值替代。")

    names = "、".join(item["name"] for item in indicators)
    warnings = [
        f"本轮取得 {len(indicators)}/{len(INDICATORS)} 项宏观指标：{names}；"
        "不含货币政策立场、分析师盈利预期与估值传导，不是完整宏观模型。",
        "CPI/PPI/M2/PMI 为统计口径的修订值，且按月发布，已按保守发布时滞收口以避免前视偏差。",
        "社融与新增信贷未包含：东方财富数据中心未提供可用的社融报表；"
        "本轮以 M2/M1 同比作为信用环境的代理观测，不等价于社融。",
        "单一利率或单个指标均不能确定股价方向；方向与强度依赖企业盈利与市场预期。",
    ] + warnings
    return {
        # items 是把各指标清单合并后的扁平列表，供覆盖率判定与节点层摘要使用；
        # 逐指标的结构化视图（含 stats 与口径）在 indicators 里。
        "items": sorted(all_items, key=lambda item: item["date"], reverse=True),
        "indicators": indicators,
        "evidence": evidence_list,
        "warnings": warnings,
        # 供节点层判定：只有全部指标都取到，宏观证据才算完整。
        "complete": len(indicators) == len(INDICATORS),
    }
