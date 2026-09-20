# 数据契约与接口

## ResearchRequest

| 字段 | 约束 / 含义 |
| --- | --- |
| symbol | 大写市场代码；演示模式仅 AAPL / MSFT / NVDA / SPY |
| question | 3–1200 字符；报告保留原问题；启用 AI 后用于综合研判 |
| mode | demo / live；必定贯穿任务与报告 |
| as_of | 2000-01-01 至今天的日历日期；真实新闻按纽约日结束时间过滤 |
| lookback_days | 30–100 条日线；不足时明确说明，不填充价格 |
| use_llm | 默认 false；启用需后端模型配置 |

请求拒绝额外未知字段。未来日期、空白问题、非法标的格式在 API 返回 422。

## 输出与证据

Bar：date、open、high、low、close、volume。价格必须有限且大于零，high/low 必须覆盖 open/close，volume 非负；分析时禁止重复日期。

Evidence：id、title、source、url、observed_at、retrieved_at、kind、is_demo、snapshot_hash、note。`observed_at` 对新闻为发布时间，对行情/宏观为观测期；**宏观观测期不等于发布时间**。

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
