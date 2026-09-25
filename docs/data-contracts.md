# 数据契约与接口

## ResearchRequest

| 字段 | 约束 / 含义 |
| --- | --- |
| symbol | 大写市场代码；演示模式仅 AAPL / MSFT / NVDA / SPY |
| question | 3–1200 字符；报告保留原问题；启用 AI 后用于综合研判 |
| mode | demo / live；必定贯穿任务与报告 |
| as_of | 2000-01-01 至今天的日历日期；真实新闻按纽约日结束时间过滤 |
| lookback_days | 30–300 条日线；取数窗口随条数放大；不足时明确说明，不填充价格 |
| use_llm | 默认 false；启用需后端模型配置 |

请求拒绝额外未知字段。未来日期、空白问题、非法标的格式在 API 返回 422。

## 输出与证据

Bar：date、open、high、low、close、volume、amount（可选，成交额，计价货币）。价格必须有限且大于零，high/low 必须覆盖 open/close，volume 非负；分析时禁止重复日期。`amount` 缺失时如实标注为不可得，不用 `volume × 均价` 估算。

Evidence：id、title、source、url、observed_at、retrieved_at、kind、is_demo、snapshot_hash、note。`kind` 取 `market`（行情）、`news`（新闻/公告）、`macro`（宏观）、`fundamental`（财务指标）四者之一；`fundamental` 由确定性财务节点产出，不对应任何角色。`observed_at` 对新闻为发布时间，对行情/宏观为观测期，对财务为最新一期公告日；**宏观观测期不等于发布时间**。

Claim：text、kind（fact / interpretation）、evidence_ids。模型输出必须符合 Pydantic / JSON Schema，每项 Claim 至少引用一个存在的证据 ID。该检查不等价于事实核查。

Report：数据模式、截止日期、摘要、指标、确定性发现、风险、分歧、限制、新闻、宏观、图表数据、证据、可选 AI 研判、用量及 Markdown。

节点输出通过其模块生产并保存为 JSON；外部行情用 Bar 校验，模型用 Synthesis 校验。没有为所有内部节点定义同一套过度宽泛的输出基类。

## API

| 方法 | 路径 | 行为 |
| --- | --- | --- |
| GET | /api/health | 检查数据库连接 |
| GET | /api/config | 返回可用模式和版本，不返回密钥 |
| POST | /api/research | 创建任务，返回 202 和任务 ID |
| GET | /api/research?limit=30&offset=0 | 分页历史记录，limit 最大 100 |
| GET | /api/research/{id} | 请求、状态、节点结果、事件、模型调用、报告 |
| POST | /api/research/{id}/cancel | 取消 queued/running 任务；已结束返回 409 |
| GET | /api/research/{id}/report.md | 下载 Markdown；未生成返回 409 |
| GET | /api/research/{id}/report.json | 结构化报告；未生成返回 409 |

创建请求可携带 `Idempotency-Key`（最长 128 字符）。同一 key 与同一规范化请求返回同一任务；相同 key 不同请求返回 409。

网页显示最近 100 条记录；更早记录可通过分页 API 获取。未实现删除记录接口，避免误删研究快照。

## 双市场扩展

请求增加 `market: CN | US`，省略为 US，兼容已有任务。CN 代码存为六位代码加交易所后缀（如 000001.SZ）；CN 不支持合成演示。报告新增 market、currency、adjustment、name、provider。日线 volume 统一为股；CN raw，US qfq。API config 的 markets 返回 CN/US；live_ready 表示无需密钥可调用，不保证上游网络状态。alpha_vantage_ready 只表示可选美国新闻/宏观密钥存在。

## 行情节点输出

`market` 节点除 `bars` 外还给出以下确定性结构，供各角色解读（数值均由 Python 计算）：

| 字段 | 含义 |
| --- | --- |
| bars_summary | `analyze()` 的聚合指标 + 逐日事实：区间高低点及日期、最大单日波动前 5 名（含 OHLC/成交量）、跳空日（明细截断为前 10 条，另给总数）、零成交日、量比、价量相关、成交额覆盖率 |
| volume_unit | 成交量单位（统一为股） |
| corporate_actions | 落在被分析窗口内的除权日公告（取自腾讯未复权日线内嵌的除权信息），含除权日、登记日、方案 |
| action_attribution | `adjusted_series_available`、最大单日波动与最近除权日的对齐结果、逐除权日的实测影响与公告 |
| cross_check | 第二源比对结果：status（consistent / mismatch / unavailable）、比对天数与区间、容差、逐日不一致明细 |

除权当日影响是**实测**值（前复权收益率 − 未复权收益率），不是按公告比例推算；对照序列与主序列日期不一致时整份丢弃，取不到时 `action_attribution.action_effect_pct` 为 `null` 而不是 0。`cross_check.status` 为 `unavailable` 表示「没去查」，与 `consistent`（查了没问题）严格区分。

## 宏观节点输出

`macro` 节点返回 `indicators`（每项含 key/name/unit/观测条数/首末日期/极值/evidence_id）与扁平化的 `items`。`complete` 为 false 表示至少一项指标本轮失败。`warnings` 写明降级原因。统计类指标（CPI/PPI/M2/M1 45 天、PMI 31 天）按发布滞后收口，历史截止日期不会读到当时尚未公布的月份。

## 财务节点输出

`fundamentals` 是确定性节点，不是角色：它不进 `AGENT_SPECS` / 角色列表 / 计划的 `enabled_agents`，只把数据放进证据池（`kind: "fundamental"`）。状态键含 `items`（最多 8 个报告期，按公告日降序）、`latest`、`evidence`、`warnings`。

| 字段 | 含义 |
| --- | --- |
| items / latest | 每期含 report_date、report_name、report_type、notice_date、currency 及营收、同比、毛利与毛利率、净利率、归母净利、扣非归母净利、加权 ROE、扣非 ROE、经营现金流、资产负债与负债率、利息保障倍数、EPS、BPS |
| cash_to_profit_pct | 派生比率（经营现金流 / 归母净利）；分子或分母不可用时为 `null`，不是 0 |

收口条件是 `NOTICE_DATE <= as_of`（**公告日**，不是报告期末），同一报告期被更正时取公告日更晚的一条；上游 `null` 与 `"--"` 一律保留为未知。取不到任何报告期时抛 `ProviderError`，不用估计值替代。美股不提供（需付费数据源），节点 `status: "skipped"` 并写明原因；A 股节点取数失败降级 `status: "partial"` 加 warning，不影响整份报告。
