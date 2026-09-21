# Atlas Research · 金融研究工作台

一个可以在本地运行的金融研究项目：选择 A 股或美股，输入股票代码，八个研究角色通过 LangGraph 条件路由协作，输出带有数据口径、风险说明和来源记录的中文报告。

**真实日线无需密钥，不需要 Docker。** 默认 A 股真实模式；美股亦可选择合成演示，合成内容持续标注。

## Windows 快速开始

要求：Python 3.11+、Node.js 22+。

1. 第一次在新电脑使用：双击根目录的 **Install-Atlas.cmd**，等待依赖安装完成。
2. 已安装依赖：双击 **Start-Atlas.cmd**。
3. 在浏览器打开 **http://localhost:3000**。
4. 选择 A 股并输入 600519，或选择美股并输入 AAPL，保留「真实数据」，点击「开始研究」。
5. 查看研究概览、证据来源和执行记录；点击「导出报告」保存 Markdown。
6. 停止服务：双击 **Stop-Atlas.cmd**。研究记录保留在 `data/research.db`。

也可以在项目根目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start.ps1 -Install
```

脚本在后台启动本项目的 API 和前端，不会关闭占用相同端口的其他程序。日志在 `data/`；重复启动会提示端口已占用。首次 Next.js 页面编译可能需要数秒。

## 已实现

- FastAPI 任务创建、状态查询、历史列表、取消任务、Markdown / JSON 报告下载。
- LangGraph 的条件路由多 Agent 图：八个角色常驻，行情、新闻和宏观取数并行，技术分析在行情后运行；研究计划决定可选角色是否启用，风控质询触发一轮返工，方向冲突时由仲裁角色裁决。
- 确定性技术指标：SMA20/50、Wilder RSI14、样本价格收益率、年化波动率、最大回撤。
- 腾讯财经公共真实日线：A 股（沪深京）与美股；股票名称、币种、价格口径与数据日期。
- Alpha Vantage 可选美国新闻摘要、月度有效联邦基金利率适配器。
- 可选 Responses API 多角色研判：每个角色有独立 system prompt 与严格 JSON 输出契约，LangChain 提示模板，结构化输出及引用 ID 校验。
- SQLite 本地存储，SQLAlchemy PostgreSQL 适配与 Docker Compose 部署配置。
- 持久化节点结果、任务租约、心跳、过期领取、取消隔离、幂等创建、模型调用预算。
- Next.js / React / TypeScript 中文工作台、响应式页面、交互价格图、来源与执行日志。
- 自动测试、版本锁定文件、Alembic 初始迁移、GitHub Actions 检查。

角色主要使用确定性程序和受控规则，**只有勾选「AI 综合研判」时各角色才调用模型**。未启用模型时，报告显示「确定性规则分析」；研究问题被保存，但不会产生针对任意问题的自由推理。

## 多 Agent 协作

Atlas Research 用八个角色完成一次研究，每个角色都有独立的 system prompt 与结构化输出契约：

- **研究经理**制定研究计划：优先用模型规划，模型不可用时降级为确定性规则规划器。计划只决定两个可选角色（事件分析、宏观分析）是否启用，其余角色常驻；计划写明每个启用角色的关注点，以及每个未启用角色的原因。
- **市场数据 / 技术分析**基于确定性取数与 Python 计算的指标工作，模型只对已校验的数据做解读，不参与取数。
- **事件分析 / 宏观分析**是可选角色，由计划决定是否启用；数据源不可用时计划会明确停用并记录原因，而不是伪造结论。
- **风控审查官**可以针对技术分析、事件分析、宏观分析提出具体质询；被质询的角色带着质询返工一轮，随后风控复审一轮。返工轮次上限为一轮，由节点名 `@1` 后缀表达。
- **首席仲裁**在确定性检测发现价格趋势方向与供应商新闻情绪标签相反时运行，并说明取舍依据；该检测不依赖模型。
- **报告撰写**汇总各角色结论。界面与导出的 Markdown 都会展示研究计划、质询与返工、仲裁结论三条轨迹。

流程骨架是确定的（取数 → 校验 → 分析 → 审查 → 汇总），但**启用哪些角色、是否需要返工、是否需要仲裁**由运行时决定，而不是预先写死。这仍然不是模型自由聊天：取数与校验始终是确定性 Python 代码，模型不自行获取数据、不调用工具，没有 ReAct 自主探索；每条证据仍带 SHA256 快照，报告不提供买卖指令或目标价。

## 接入真实数据与模型

行情默认可用。需要美国新闻/宏观或 AI 时，复制 `.env.example` 为 `.env`，只在后端配置相应密钥：

```dotenv
ALPHA_VANTAGE_API_KEY=你的数据服务密钥
OPENAI_API_KEY=你的模型密钥
OPENAI_MODEL=你的API账号可用且支持结构化输出的模型名称
OPENAI_BASE_URL=https://api.openai.com/v1
```

重启后端、刷新页面，对应开关会启用。`OPENAI_BASE_URL` 可使用支持 Responses API 和 JSON Schema 的 HTTPS 服务；**仅支持 Chat Completions 的兼容服务不能直接使用**。

- 真实数据与模型开关独立，可以只使用真实行情而不调用模型。
- 真实取数失败不会替换成模拟数据。行情失败使任务失败；新闻/宏观不可用时输出「部分完成」报告。
- Alpha Vantage 的额度、速率和接口付费权限由供应商决定。行情走腾讯公共接口，Alpha Vantage 仅用于可选美国新闻/宏观；请避免短时间反复提交。
- 模型默认每任务最多 16 次调用、每次调用最多 16000 输出 tokens；没有隐式模型重试。一次完整研究最坏消耗 12 次（初轮 8 个角色 + 一轮返工 4 个），余量留给降级路径。中断后已预留的调用也计入预算，以免重复计费。
- 若模型是推理模型（如 `deepseek-v4-flash`），`max_output_tokens` 是**推理与正文共享的预算**，按非推理模型的直觉给值会导致输出被截断。`HTTP_TIMEOUT_SECONDS`、`TASK_TIMEOUT_SECONDS`、`MAX_LLM_OUTPUT_TOKENS`、`MAX_LLM_CALLS` 是一组联动配置，改其中一个要连带检查其余几个：放宽单次超时会拉长整条链路的墙钟时间，任务级超时必须同步放宽，否则失败形态会从「单次截断」变成「整个任务被砍」。
- 实际 token 用量在成功响应后记录。配置输入/输出单价后才显示估算费用；未配置时显示「未配置单价」。调用失败或断网不代表未计费。
- 所有研究数据与问题会在开启 AI 时发给配置的模型服务。浏览器不接收 API 密钥。

## 数据口径

研究范围为 A 股（CNY）及美国股票 / ETF（USD），已收盘日线。A 股接受 `600519`、`sh600519`、`600519.SH`，保留前导零并校验交易所；美股如 `AAPL`、`IBM`。北交所等标的历史覆盖依赖供应商，少于 20 条有效日线明确失败。

- `lookback_days` 表示**日线条数**，范围 30–100，不是自然日。请求截止日前 400 个自然日内的历史数据并截取所需条数；历史覆盖不足会明确提示。
- 行情截止日期按各市场时区处理；A 股 15:15 上海时间、美股 16:15 纽约时间之前不采用当日日线。美国新闻不超过该日结束时间或当前时间。未实现完整交易所日历或半日交易日规则。
- A 股为未复权价格；美股为供应商当前版本前复权价格，历史值可能修订，不能用于严格时点回测。拆股、分红可能影响统计。没有总回报、财报估值、组合风险或策略回测功能。
- A 股新闻与公司公告已接入东方财富公开接口，无需密钥；中国宏观取自东方财富数据中心的 10 年期国债到期收益率，同样无需密钥。
- A 股新闻与公告取截止日前 90 天的有限检索结果，每源最多 8 条，按发布时间过滤及去重。新闻仅为检索片段，关键词结果可能含市场综述；公告只读取标题索引，不解析正文。单源失败保留另一来源并标明不完整。
- 美国新闻来自聚合摘要，默认过去 30 个自然日，最多 12 条去重事件。不抓取原文全文、不保证覆盖所有重大事件。
- A 股宏观只覆盖中国 10 年期国债到期收益率，按月取当月最后一个可得观测（至多 12 个），仅作长期无风险利率／折现率代理，不含货币政策立场、通胀与盈利预期。收益率是市场成交观测、按日发布且不在事后改写，因此历史截止日期照常取数，不需要像联邦基金利率那样跳过。
- 美国宏观沿用 Alpha Vantage 月度有效联邦基金利率，不能代表完整宏观研究。由于数据源缺少历史发布时间和历史修订版本，历史截止日期会跳过真实宏观模块。
- 演示行情使用确定性公式，只排除周末，未模拟节假日。演示新闻和 4.25% 利率设定均为虚构。
- 每项证据记录来源、观测/发布时间、采集时间和标准化数据的 SHA256；这是本地可追溯性校验，不是来源真实性证明。
- AI 引用校验只证明引用 ID 存在，不能自动证明模型的每句推断成立。AI 研判在报告中单独标注。

## 手动开发启动

从项目根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend/requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e backend
.\.venv\Scripts\python.exe -m uvicorn financial_research.api:app --host 127.0.0.1 --port 8765
```

另开一个终端：

```powershell
cd frontend
npm ci
npm run dev
```

macOS / Linux：Python 路径改为 `.venv/bin/python`，其余命令相同。API 文档在 http://localhost:8765/docs。前端将 `/api/*` 转发给 `http://127.0.0.1:8765`，无需开放跨域。默认避开部分 Windows 系统预留的 8000 端口。

独立 Worker：将 `EMBED_WORKER=false` 写入 `.env`，另运行 `python -m financial_research.worker`。默认本地启动在 API 进程中启动一个 Worker 线程；多任务顺序领取，单个任务内按图并行处理。

离线生成报告：

```powershell
.\.venv\Scripts\python.exe -m financial_research.cli demo --symbol AAPL --as-of 2026-09-18 --output data/demo-report.md
```

## 测试与构建

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests -q
.\.venv\Scripts\python.exe -m ruff check backend
.\.venv\Scripts\python.exe -m ruff format --check backend
cd frontend
npm run typecheck
npm run build
```

测试不调用真实市场服务或付费模型，使用固定样例与 HTTP MockTransport。覆盖指标参考数值、日期过滤、去重、上游错误、模型拒绝/非法引用、任务恢复、抢占、取消、预算和 API 完整流程。第三方库的弃用提示不影响当前功能。

生产构建后可用 `npm run start`；`BACKEND_URL` 是构建时配置，改变后端地址时需重新构建。

## Docker + PostgreSQL

安装 Docker Desktop 后，在项目根目录运行：

```sh
docker compose up --build -d
```

Compose 先等待 PostgreSQL 健康，再执行数据库迁移，随后启动 API、独立 Worker、Next.js。只向本机发布 3000 / 8877 端口，数据库使用命名卷持久保存。

```sh
docker compose logs -f worker
docker compose down
```

`docker compose down` 不删除数据库卷。当前是本地单用户系统，没有用户登录、租户隔离或公开服务限流；如需对外部署，应另加认证、HTTPS、凭据管理及数据库备份。

迁移命令：

```powershell
.\.venv\Scripts\python.exe -m alembic -c backend/alembic.ini upgrade head
```

初始迁移可接管 v0.1 自动创建的本地数据库。以后修改表结构必须增加迁移；启动时 `create_all` 不会自动修改已有列。更新前备份数据库。

## 目录与设计

```text
backend/src/financial_research/  # API、工作流、数据源、模型、指标、存储、Worker
backend/tests/                  # 离线测试
backend/migrations/             # 版本化数据库迁移
frontend/                       # Next.js 工作台
infra/                          # 后端镜像
scripts/                        # Windows 启停脚本
docs/                           # 架构、数据契约、验收记录
data/                           # 本地数据库、日志、报告（不提交版本库）
```

更多设计见 [架构说明](docs/architecture.md)、[数据契约](docs/data-contracts.md)、[验收记录](docs/verification.md)。
