"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Activity, ArrowDownToLine, ArrowRight, ArrowUpRight, BookOpen, Check, CheckCircle2, ChevronRight,
  Circle, CircleHelp, Clock3, Compass, Database, FileText, FlaskConical, Globe2, Layers3, LayoutDashboard,
  LoaderCircle, PanelLeftClose, Plus, Radio, Search, Settings2, ShieldCheck, Sparkles, Square, TrendingUp, X } from "lucide-react";
import type { Bar, Config, Detail, Evidence, Report, ResearchRequest, Status, Task } from "@/lib/types";

const AGENTS = [
  ["manager", "研究经理", "规划研究路径"], ["market", "市场数据", "获取与校验行情"],
  ["technical", "技术分析", "计算趋势与动量"], ["news", "新闻事件", "整理事件与来源"],
  ["macro", "宏观研究", "观察利率环境"], ["risk", "风险审查", "识别缺口与分歧"], ["report", "研究报告", "汇总结论与证据"],
];
const LABELS: Record<string, string> = { queued: "等待执行", running: "研究中", completed: "已完成", partial: "部分完成", failed: "执行失败", cancelled: "已取消", skipped: "已跳过" };
const TERMINAL = new Set(["completed", "partial", "failed", "cancelled"]);
const today = () => new Date().toLocaleDateString("sv-SE");
const dateLabel = (value: string) => new Date(value).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
const fmt = (n: number | null | undefined, digits = 2) => n == null ? "—" : n.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });

async function api<T>(url: string, options?: RequestInit): Promise<T> {
  const res = await fetch(url, { ...options, headers: { "Content-Type": "application/json", ...options?.headers }, cache: "no-store" });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = data.detail;
    throw new Error(typeof detail === "string" ? detail : Array.isArray(detail) ? detail.map((x: {msg: string}) => x.msg).join("；") : "服务暂时不可用，请检查后端是否启动。");
  }
  return data;
}

function StatusBadge({ status }: { status: Status }) {
  return <span className={`badge ${status}`}><span className="badge-dot" />{LABELS[status] || status}</span>;
}

function PriceChart({ bars, demo, currency, adjustment }: { bars: Bar[]; demo: boolean; currency: string; adjustment: string }) {
  const [hover, setHover] = useState<number | null>(null);
  if (!bars.length) return null;
  const w = 800, h = 238, left = 12, right = 62, top = 16, bottom = 28;
  const values = bars.map(x => x.close);
  const ma = values.map((_, i) => i < 19 ? null : values.slice(i - 19, i + 1).reduce((a, b) => a + b, 0) / 20);
  const low = Math.min(...values), high = Math.max(...values);
  const pad = (high - low) * .18 || 1, min = low - pad, max = high + pad;
  const x = (i: number) => left + i / Math.max(1, bars.length - 1) * (w - left - right);
  const y = (v: number) => top + (max - v) / (max - min) * (h - top - bottom);
  const line = values.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(2)},${y(v).toFixed(2)}`).join(" ");
  const moving = ma.map((v, i) => v === null ? "" : `${i === 19 ? "M" : "L"}${x(i)},${y(v)}`).join(" ");
  const active = hover === null ? bars.length - 1 : Math.min(hover, bars.length - 1);
  return <div className="chart-wrap">
    <div className="chart-hover"><span>{bars[active].date}</span><strong>{currency === "CNY" ? "¥" : "$"}{fmt(bars[active].close)}</strong><span className="muted">{demo ? "合成收盘价" : adjustment === "qfq" ? "前复权收盘价" : "未复权收盘价"}</span></div>
    <svg viewBox={`0 0 ${w} ${h}`} role="img" aria-label={`${demo ? "合成演示" : "真实"}日线收盘价图，${bars[0].date} 至 ${bars[bars.length - 1].date}`}
      onMouseLeave={() => setHover(null)} onMouseMove={e => { const rect = e.currentTarget.getBoundingClientRect(); setHover(Math.max(0, Math.min(bars.length - 1, Math.round(((e.clientX - rect.left) / rect.width * w - left) / (w - left - right) * (bars.length - 1))))); }}>
      <defs><linearGradient id="price-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#18816d" stopOpacity=".17" /><stop offset="100%" stopColor="#18816d" stopOpacity="0" /></linearGradient></defs>
      {[0, 1, 2, 3].map(i => { const v = min + (max - min) * i / 3; return <g key={i}><line x1={left} x2={w-right+5} y1={y(v)} y2={y(v)} stroke="#e9eeec" strokeDasharray="4 5" /><text x={w-right+13} y={y(v)+4} className="chart-label">{fmt(v, 0)}</text></g>; })}
      <path d={`${line} L${x(bars.length-1)},${h-bottom} L${left},${h-bottom} Z`} fill="url(#price-fill)" />
      <path d={moving} fill="none" stroke="#c5a26a" strokeWidth="1.6" strokeDasharray="5 4" />
      <path d={line} fill="none" stroke="#16806c" strokeWidth="2.4" strokeLinejoin="round" />
      {[0, Math.floor(bars.length/3), Math.floor(bars.length*2/3), bars.length-1].map((i, j) => <text key={i} x={x(i)} y={h-4} textAnchor={j===0 ? "start" : j===3 ? "end" : "middle"} className="chart-label">{bars[i].date.slice(5)}</text>)}
      {hover !== null && <g><line x1={x(active)} x2={x(active)} y1={top} y2={h-bottom} stroke="#16806c" strokeDasharray="3 4" opacity=".5" /><circle cx={x(active)} cy={y(values[active])} r="4" fill="#16806c" stroke="white" strokeWidth="2" /></g>}
    </svg>
  </div>;
}

function Sources({ evidence }: { evidence: Evidence[] }) {
  return <div className="sources">{evidence.map((ev, i) => <article className="source" key={ev.id} id={ev.id}>
    <div className="source-number">{String(i + 1).padStart(2, "0")}</div><div className="source-body"><div className="source-meta"><span>{ev.source}</span><span>{ev.kind === "market" ? "行情" : ev.kind === "macro" ? "宏观" : "新闻"}</span>{ev.is_demo && <span className="demo-tag">合成演示</span>}</div>
      <h3>{ev.title}</h3><p>{ev.note || "保留原始来源和采集时间，可通过快照标识追溯。"}</p><div className="source-foot">观测 / 发布时间 {ev.observed_at.slice(0, 19).replace("T", " ")} · 采集 {dateLabel(ev.retrieved_at)}</div>
      <details><summary>查看证据标识与快照校验值</summary><code>{ev.id}<br />SHA256 {ev.snapshot_hash}</code></details></div>
    {ev.url && /^https?:\/\//.test(ev.url) && <a className="icon-button" href={ev.url} target="_blank" rel="noreferrer" aria-label={`打开来源：${ev.title}`}><ArrowUpRight size={18} /></a>}
  </article>)}</div>;
}

function ReportBody({ report, showSources }: { report: Report; showSources: () => void }) {
  return <>
    <div className={`data-notice ${report.mode === "demo" ? "" : "real"}`}><FlaskConical size={16} /><span>{report.mode === "demo" ? "演示报告 · 行情、新闻和宏观数据均为合成情景，不反映真实市场。" : "真实日线 · 供应商数据可能延迟；仅纳入已收盘数据，请关注价格口径和缺失项。"}</span></div>
    <div className="metric-grid">
      <div className="metric"><span>最后收盘价 <small>{report.currency || "USD"}</small></span><strong>{fmt(report.metrics.last_close)}</strong><small>截至 {report.metrics.last_date}</small></div>
      <div className="metric"><span>样本区间变化</span><strong className={report.metrics.period_return_pct >= 0 ? "positive" : "negative"}>{report.metrics.period_return_pct > 0 ? "+" : ""}{fmt(report.metrics.period_return_pct)}<em>%</em></strong><small>{report.metrics.sample_size} 个日线样本</small></div>
      <div className="metric"><span>年化历史波动</span><strong>{fmt(report.metrics.volatility_pct)}<em>%</em></strong><small>基于 252 个交易日年化</small></div>
      <div className="metric"><span>最大样本回撤</span><strong>{fmt(report.metrics.max_drawdown_pct)}<em>%</em></strong><small>样本收盘价峰谷跌幅</small></div>
    </div>
    <div className="analysis-grid"><section className="panel price-panel"><div className="panel-head"><h3><TrendingUp size={17} /> 价格与趋势</h3><div className="chart-legend"><i />收盘价 <i className="gold" />SMA20</div></div><PriceChart bars={report.chart} demo={report.mode === "demo"} currency={report.currency || "USD"} adjustment={report.adjustment || "raw"} /><div className="indicator-row"><span>趋势 <b>{report.metrics.trend}</b></span><span>RSI 14 <b>{fmt(report.metrics.rsi14)}</b></span><span>SMA 50 <b>{fmt(report.metrics.sma50)}</b></span></div></section>
      <section className="panel insight-panel"><div className="panel-head"><h3><Compass size={17} /> 研究摘要</h3><span className="tiny-label">SYNTHESIS</span></div><p className="summary-text">{report.summary}</p><div className="summary-foot"><span><Layers3 size={14} /> {report.evidence.length} 项证据</span><button onClick={showSources}>查看来源 <ArrowUpRight size={14} /></button></div><div className="engine-label"><CheckCircle2 size={14} />{report.engine}</div></section></div>
    <section className="panel narrative"><div className="panel-head"><h3><BookOpen size={17} /> 研究观察</h3><span className="tiny-label">EVIDENCE FIRST</span></div><div className="observations">{report.claims.map((c, i) => <div className="observation" key={i}><span className="obs-index">0{i + 1}</span><div><span className="claim-type">{c.kind === "fact" ? "数据观察" : "规则解释"}</span><p>{c.text}</p><button className="citation" onClick={showSources}>{c.evidence_ids.join(" · ")} <ArrowUpRight size={11} /></button></div></div>)}</div></section>
    <div className="analysis-grid secondary"><section className="panel"><div className="panel-head"><h3><Globe2 size={17} /> 新闻与公告</h3><span className="count">{report.news.length}</span></div>{report.news.length ? report.news.map((item, i) => <article className="news-item" key={i}><div className="news-date">{item.published_at.slice(0, 10)} · {item.category === "announcement" ? "公司公告" : "新闻"} <span>{item.source}</span></div><h4>{item.title}</h4><p>{item.summary}</p>{item.url && /^https?:\/\//.test(item.url) && <a href={item.url} target="_blank" rel="noreferrer">阅读来源 <ArrowUpRight size={12} /></a>}</article>) : <p className="empty-inline">该区间没有可用新闻证据。详细原因见数据限制。</p>}</section>
      <div className="stack"><section className="panel macro-panel"><div className="panel-head"><h3><Activity size={17} /> 宏观背景</h3></div><p>{report.macro.summary}</p>{report.macro.interpretation && <p className="muted">{report.macro.interpretation}</p>}</section><section className="panel risk-panel"><div className="panel-head"><h3><ShieldCheck size={17} /> 风险与分歧</h3><span className="count amber">{report.risks.length}</span></div>{report.risks.map((r, i) => <div className={`risk-item ${r.level}`} key={i}><i /><div><h4>{r.title}</h4><p>{r.detail}</p></div></div>)}{report.conflicts.map((c, i) => <p className="conflict" key={i}>{c}</p>)}</section></div></div>
    {report.ai_synthesis && <section className="panel ai-panel"><div className="panel-head"><h3><Sparkles size={17} /> AI 综合研判</h3><span className="tiny-label">需结合证据审阅</span></div><p>{report.ai_synthesis.summary}</p>{report.ai_synthesis.claims.map((c, i) => <div className="ai-claim" key={i}><p>{c.text}</p><button className="citation" onClick={showSources}>{c.evidence_ids.join(" · ")}</button></div>)}<ul>{report.ai_synthesis.uncertainties.map((x, i) => <li key={i}>{x}</li>)}</ul>{report.usage && <small>输入 {report.usage.input_tokens} / 输出 {report.usage.output_tokens} tokens · 估算费用 {report.usage.estimated_cost_usd == null ? "未配置单价" : `$${report.usage.estimated_cost_usd.toFixed(4)}`}</small>}</section>}
    <details className="limitations"><summary><CircleHelp size={15} /> 数据口径与研究限制 <span>{report.limitations.length} 项</span></summary><ul>{report.limitations.map((x, i) => <li key={i}>{x}</li>)}</ul><p>{report.metrics.methodology}</p></details>
  </>;
}

export default function Home() {
  const [config, setConfig] = useState<Config | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [view, setView] = useState("workspace");
  const [tab, setTab] = useState("overview");
  const [form, setForm] = useState<ResearchRequest>({ market: "CN", symbol: "600519", question: "分析近期价格趋势、新闻催化因素与主要风险。", mode: "live", as_of: "", lookback_days: 90, use_llm: false });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [filter, setFilter] = useState("");
  const [help, setHelp] = useState(false);
  const selectionRef = useRef<string | null>(null);

  const selectTask = useCallback((id: string) => { selectionRef.current = id; setSelected(id); setDetail(null); setTab("overview"); setView("workspace"); }, []);
  const refreshList = useCallback(async () => { const result = await api<{items: Task[]}>("/api/research?limit=100"); setTasks(result.items); return result.items; }, []);
  useEffect(() => {
    let active = true;
    setForm(f => ({ ...f, as_of: today() }));
    Promise.all([api<Config>("/api/config"), api<{items: Task[]}>("/api/research?limit=100")]).then(([c, list]) => {
      if (!active) return; setConfig(c); setTasks(list.items);
      if (!selectionRef.current && list.items.length) selectTask(list.items[0].id);
    }).catch(e => active && setError(e.message));
    return () => { active = false; };
  }, [selectTask]);
  useEffect(() => {
    if (!selected) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const result = await api<Detail>(`/api/research/${selected}`);
        if (!active) return;
        setDetail(result);
        if (!TERMINAL.has(result.status)) timer = setTimeout(poll, 1000);
        else await refreshList();
      } catch (e) { if (active) { setError((e as Error).message); timer = setTimeout(poll, 5000); } }
    };
    poll();
    return () => { active = false; clearTimeout(timer); };
  }, [selected, refreshList]);

  async function submit(e?: React.FormEvent) {
    e?.preventDefault(); setBusy(true); setError("");
    try {
      const result = await api<{id: string}>("/api/research", { method: "POST", body: JSON.stringify(form), headers: { "Idempotency-Key": crypto.randomUUID() } });
      selectTask(result.id); await refreshList();
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  async function cancel() {
    if (!selected) return;
    try { await api(`/api/research/${selected}/cancel`, {method: "POST"}); setDetail(await api<Detail>(`/api/research/${selected}`)); await refreshList(); }
    catch (e) { setError((e as Error).message); }
  }
  function newResearch() { setView("workspace"); setSelected(null); selectionRef.current = null; setDetail(null); setTab("overview"); setTimeout(() => document.getElementById("symbol")?.focus(), 0); }
  const active = !!detail && !TERMINAL.has(detail.status);
  const report = detail && ["completed", "partial"].includes(detail.status) ? detail.report : null;
  const completed = tasks.filter(t => ["completed", "partial"].includes(t.status)).length;

  return <div className="app-shell">
    <aside className="sidebar"><a href="/" className="brand"><span className="brand-mark"><Compass size={23} strokeWidth={1.6} /></span><span>atlas<span className="brand-sub">RESEARCH</span></span></a>
      <div className="workspace-label"><span className="workspace-avatar">A</span><div>个人研究工作区<small>LOCAL WORKSPACE</small></div><PanelLeftClose size={15} /></div>
      <button className="new-button" onClick={newResearch}><Plus size={17} /> 新建研究 <span>↗</span></button>
      <span className="nav-label">工作空间</span><nav><button className={view === "workspace" ? "nav-item selected" : "nav-item"} onClick={() => setView("workspace")}><LayoutDashboard size={17} /> 研究工作台 <span className="nav-dot" /></button><button className={view === "history" ? "nav-item selected" : "nav-item"} onClick={() => { setView("history"); refreshList().catch(e => setError(e.message)); }}><Clock3 size={17} /> 研究记录 <span className="nav-count">{tasks.length}</span></button><button className={view === "settings" ? "nav-item selected" : "nav-item"} onClick={() => setView("settings")}><Settings2 size={17} /> 数据与模型</button></nav>
      <div className="recent-head"><span className="nav-label">最近研究</span><FileText size={13} /></div><div className="recent-list">{tasks.slice(0, 6).map(task => <button key={task.id} className={`recent-item ${selected === task.id && view === "workspace" ? "current" : ""}`} onClick={() => selectTask(task.id)}><span className={`recent-dot ${task.status}`} /><div><strong>{task.request.symbol}<span>{task.request.mode === "demo" ? "演示" : "真实"}</span></strong><small>{dateLabel(task.created_at)}</small></div><ChevronRight size={13} /></button>)}{!tasks.length && <p className="no-recent">你的第一份研究将在这里出现。</p>}</div>
      <div className="sidebar-bottom"><div className="local-status"><span />本地运行 <small>v{config?.version || "0.1.0"}</small></div><button onClick={() => setHelp(true)}><CircleHelp size={16} /> 快速开始 <ArrowUpRight size={14} /></button><div className="profile"><span>研</span><div>独立研究者<small>个人工作区</small></div></div></div>
    </aside>
    <div className="main-shell"><header className="topbar"><div><span>工作空间</span><ChevronRight size={13} /><strong>{view === "history" ? "研究记录" : view === "settings" ? "数据与模型" : "研究工作台"}</strong></div><div className="topbar-right"><span className="local-pill"><span />{config ? "服务已连接" : "连接服务中"}</span><button className="icon-button" onClick={() => setHelp(true)} aria-label="使用帮助"><CircleHelp size={18} /></button></div></header>
      <main>{error && <div className="error-banner" role="alert"><span>{error}</span><button className="icon-button" onClick={() => setError("")} aria-label="关闭错误提示"><X size={16} /></button></div>}
      {view === "workspace" && <><div className="page-heading"><div><div className="eyebrow"><span /> RESEARCH, WITH PERSPECTIVE</div><h1>让每一个判断，都有据可循<span>。</span></h1><p>从市场数据到研究结论，七个研究角色为你梳理趋势、事件与风险。</p></div><span className="heading-badge"><Layers3 size={16} /> 7 个协作角色</span></div>
      <div className="workspace-grid"><section className="panel request-panel"><div className="panel-head"><h2><Search size={17} /> 发起一项研究</h2><span className="tiny-label">01 / BRIEF</span></div><form onSubmit={submit}><label htmlFor="market">交易市场</label><select id="market" value={form.market || "US"} onChange={e => setForm({...form, market: e.target.value as "CN" | "US", symbol: e.target.value === "CN" ? "600519" : "AAPL", mode: "live"})}><option value="CN">A 股 · 沪深京 · CNY</option><option value="US">美股 · USD</option></select><label htmlFor="symbol">研究标的 <span>{form.market === "CN" ? "六位代码 / 交易所后缀" : "美股代码"}</span></label><div className="symbol-input"><Search size={17} /><input id="symbol" value={form.symbol} onChange={e => setForm({...form, symbol: e.target.value.toUpperCase()})} maxLength={12} required autoComplete="off" aria-describedby="symbol-hint" /><span>{form.market === "CN" ? "CNY" : "USD"}</span></div><div className="quick-symbols" id="symbol-hint">{(form.market === "CN" ? ["600519", "000001", "300750", "688981"] : ["AAPL", "MSFT", "NVDA", "SPY"]).map(s => <button key={s} type="button" className={form.symbol === s ? "active" : ""} onClick={() => setForm({...form, symbol: s})}>{s}</button>)}</div>
        <label htmlFor="question">你希望了解什么？</label><textarea id="question" rows={3} value={form.question} onChange={e => setForm({...form, question: e.target.value})} minLength={3} maxLength={1200} required />
        <div className="form-row"><div><label htmlFor="as-of">数据截止日期</label><input id="as-of" type="date" value={form.as_of} min="2000-01-01" max={today()} required onChange={e => setForm({...form, as_of: e.target.value})} /></div><div><label htmlFor="lookback">日线样本</label><select id="lookback" value={form.lookback_days} onChange={e => setForm({...form, lookback_days: Number(e.target.value)})}><option value={30}>30 个交易日</option><option value={60}>60 个交易日</option><option value={90}>90 个交易日</option><option value={100}>100 个交易日</option></select></div></div>
        <label>数据模式</label><div className="segmented"><button type="button" className={form.mode === "demo" ? "on" : ""} disabled={form.market === "CN"} title={form.market === "CN" ? "A 股使用真实行情；演示样例仅支持美股" : "合成演示"} onClick={() => setForm({...form, mode: "demo"})}><FlaskConical size={14} /> 合成演示</button><button type="button" className={form.mode === "live" ? "on" : ""} disabled={!config?.live_ready} title={!config?.live_ready ? "行情服务尚未就绪" : "使用真实数据"} onClick={() => setForm({...form, mode: "live"})}><Radio size={14} /> 真实数据</button></div>
        <label className={`ai-toggle ${!config?.llm_ready ? "disabled" : ""}`}><span><Sparkles size={15} /> AI 综合研判 <small>{config?.llm_ready ? "按需调用模型" : "配置模型后可开启"}</small></span><input type="checkbox" checked={form.use_llm} disabled={!config?.llm_ready} onChange={e => setForm({...form, use_llm: e.target.checked})} /></label>
        <button className="primary-button" type="submit" disabled={busy || !config || !form.as_of}>{busy ? <LoaderCircle className="spin" size={17} /> : <Sparkles size={17} />}{busy ? "正在创建任务…" : "开始研究"}<ArrowRight size={16} /></button><p className="form-note">{form.mode === "demo" ? "无需 API 密钥 · 使用可复现的合成数据" : "无需行情密钥 · 已收盘日线 · 非逐笔实时"}</p></form></section>
      <section className="panel team-panel"><div className="panel-head"><h2><Layers3 size={17} /> 研究协作流程</h2>{detail ? <StatusBadge status={detail.status} /> : <span className="badge idle"><span className="badge-dot" />准备就绪</span>}</div><div className="team-intro"><h3>{active ? "正在连接线索，形成研究。" : report ? "研究已就绪，证据已归档。" : "一个问题，七个研究视角。"}</h3><p>{detail?.error || (detail?.status === "cancelled" ? "任务已取消。你可以修改研究条件，重新发起。" : "每个角色独立处理任务，再由风险审查与报告角色汇合结果。")}</p></div><div className="agent-grid">{AGENTS.map(([key, name, desc], i) => { const agent = detail?.agents.find(a => a.name === key); return <div className={`agent-card ${agent?.status || "idle"}`} key={key}><div className="agent-top"><span className="agent-number">0{i + 1}</span>{agent?.status === "running" ? <LoaderCircle size={15} className="spin" /> : agent?.status === "completed" ? <CheckCircle2 size={15} /> : agent?.status === "partial" ? <CircleHelp size={15} /> : agent?.status === "failed" ? <X size={15} /> : <Circle size={13} />}</div><h4>{name}</h4><p>{desc}</p><small>{agent ? LABELS[agent.status] : "待命"}{agent?.duration_ms != null ? ` · ${agent.duration_ms < 1000 ? `${agent.duration_ms}ms` : `${(agent.duration_ms / 1000).toFixed(1)}s`}` : ""}</small></div>; })}<div className="agent-card outcome"><FileText size={20} /><strong>可追溯报告</strong><p>观点 · 风险 · 来源</p><small>Markdown / JSON</small></div></div><div className="team-footer"><span><ShieldCheck size={15} />数据校验 → 并行分析 → 风险审查 → 报告</span>{active && <button onClick={cancel}><Square size={12} /> 取消任务</button>}</div></section></div>
      <div className="results-heading"><div><h2>{report ? `${report.name || report.symbol} · ${report.symbol} 研究结果` : "研究结果"}</h2><span>{report ? `截止 ${report.as_of} · ${report.market === "CN" ? "A 股 / CNY" : "美股 / USD"} · ${report.adjustment === "qfq" ? "前复权" : report.mode === "live" ? "未复权" : "合成"} · ${report.mode === "demo" ? "合成演示" : "真实数据"}` : "你的下一份市场洞察，从这里开始"}</span></div>{report && <a className="secondary-button" href={`/api/research/${selected}/report.md`}><ArrowDownToLine size={15} /> 导出报告</a>}</div>
      {detail && <div className="tabs" role="tablist" aria-label="研究结果分类">{[["overview", "研究概览"], ["sources", `证据来源${report ? ` · ${report.evidence.length}` : ""}`], ["activity", "执行记录"]].map(([key, label]) => <button role="tab" aria-selected={tab === key} onClick={() => setTab(key)} key={key}>{label}</button>)}</div>}
      {tab === "activity" && detail ? <section className="panel activity-panel"><div className="panel-head"><h3><Clock3 size={17} /> 执行记录</h3><span>第 {detail.attempts} 次执行</span></div>{detail.events.map(event => <div className="event-row" key={event.id}><span className="event-dot" /><time>{dateLabel(event.created_at)}</time><strong>{event.kind}</strong><span>{event.message}</span></div>)}</section> : tab === "sources" && report ? <section className="panel"><Sources evidence={report.evidence} /></section> : report ? <ReportBody report={report} showSources={() => setTab("sources")} /> : <div className="empty-report"><div className="empty-illustration"><div className="paper"><span /><span /><span /><svg viewBox="0 0 100 42"><path d="M4 34 L20 27 L35 31 L51 15 L66 20 L81 7 L97 11" /></svg></div><span className="empty-compass"><Compass size={26} /></span></div><h3>{active ? "研究正在进行" : detail?.status === "failed" ? "这次研究未能完成" : "等待你的第一个研究问题"}</h3><p>{active ? "各角色的执行状态会自动更新，完成后报告将显示在这里。" : detail?.error || "选择标的，点击「开始研究」，查看一份附有证据与风险说明的报告。"}</p><div className="empty-features"><span><Check size={13} />结构化研究</span><span><Check size={13} />来源可追溯</span><span><Check size={13} />随时导出</span></div></div>}
      </>}
      {view === "history" && <><div className="page-heading"><div><div className="eyebrow">YOUR RESEARCH LIBRARY</div><h1>每一次研究，留下依据。</h1><p>已加载最近 {tasks.length} 项研究，其中 {completed} 项已有报告。</p></div><button className="primary-button compact" onClick={newResearch}><Plus size={16} /> 新建研究</button></div><div className="history-search"><Search size={17} /><input aria-label="搜索研究记录" placeholder="搜索股票代码或研究问题…" value={filter} onChange={e => setFilter(e.target.value)} /></div><section className="panel history-panel"><div className="history-row history-header"><span>研究标的 / 问题</span><span>数据模式</span><span>创建时间</span><span>状态</span><span /></div>{tasks.filter(t => `${t.request.symbol} ${t.request.question}`.toLowerCase().includes(filter.toLowerCase())).map(t => <button className="history-row" key={t.id} onClick={() => selectTask(t.id)}><div><strong>{t.request.symbol}</strong><p>{t.request.question}</p></div><span>{t.request.mode === "demo" ? "合成演示" : "真实数据"}</span><span>{dateLabel(t.created_at)}</span><StatusBadge status={t.status} /><ArrowUpRight size={16} /></button>)}{!tasks.length && <p className="empty-inline">还没有研究记录。开始第一项研究后，会自动保存在这里。</p>}</section></>}
      {view === "settings" && <><div className="page-heading"><div><div className="eyebrow">CONNECTIONS & CAPABILITIES</div><h1>研究能力，按需连接。</h1><p>凭据只保存在后端环境配置中，不发送到浏览器。</p></div></div><div className="settings-grid">{[
        {icon: FlaskConical, name: "合成数据", badge: "开箱即用", ready: true, desc: "可复现的行情、虚构新闻与宏观情景。适合了解工作流与验证功能。", vars: "无需配置任何密钥"},
        {icon: Database, name: "腾讯财经真实行情", badge: "无需密钥", ready: true, desc: "A 股（沪深京）和美股已收盘日线。A 股未复权，美股前复权；公共接口可能延迟或存在历史覆盖缺口。", vars: "默认开启 · CN / US"},
        {icon: Globe2, name: "A 股新闻与公告", badge: "无需密钥", ready: true, desc: "东方财富个股新闻检索片段与公司公告索引，近 90 天有限结果，保留原始链接；不代表已阅读全文。", vars: "默认开启 · 新闻 / 公司公告"},
        {icon: Globe2, name: "美国新闻与宏观（可选）", badge: config?.alpha_vantage_ready ? "已配置" : "待配置", ready: config?.alpha_vantage_ready, desc: "Alpha Vantage 提供美国新闻和利率背景。未配置时行情研究仍可运行，报告标记证据缺失。A 股新闻与公告使用免费公开接口；中国宏观尚未接入。", vars: "ALPHA_VANTAGE_API_KEY=你的密钥"},
        {icon: Sparkles, name: "AI 综合研判", badge: config?.llm_ready ? "已配置" : "可选连接", ready: config?.llm_ready, desc: "在确定性分析基础上，使用 Responses API 生成有引用的中文研判；每项任务默认最多调用一次。", vars: "OPENAI_API_KEY=你的密钥\nOPENAI_MODEL=账号可用的模型名称"},
      ].map(c => <section className="panel connection-card" key={c.name}><div className="connection-icon"><c.icon size={23} /></div><span className={`badge ${c.ready ? "completed" : "idle"}`}><span className="badge-dot" />{c.badge}</span><h2>{c.name}</h2><p>{c.desc}</p><pre>{c.vars}</pre></section>)}</div><section className="panel setup-guide"><h3>真实行情与可选增强</h3><ol><li>在项目根目录复制 <code>.env.example</code> 为 <code>.env</code>。</li><li>真实日线已默认开启。美国新闻与宏观可填写 Alpha Vantage 密钥；AI 研判另需模型密钥与名称。</li><li>重启后端并刷新页面，对应开关会自动启用。</li></ol><p>“已配置”只代表存在配置，不代表远程服务已验证。不要把密钥粘贴到研究问题中。</p></section></>}
      <footer className="page-footer"><span><Compass size={13} /> ATLAS RESEARCH</span><span>有依据的观察，可追溯的研究。</span><span>Local-first · v0.1.0</span></footer></main>
    </div>
    {help && <div className="modal-overlay" onClick={() => setHelp(false)}><section className="help-modal" role="dialog" aria-modal="true" aria-labelledby="help-title" onClick={e => e.stopPropagation()}><button className="icon-button modal-close" onClick={() => setHelp(false)} aria-label="关闭帮助"><X size={20} /></button><div className="connection-icon"><Compass size={27} /></div><h2 id="help-title">从第一份研究开始</h2><p>无需行情密钥。选择 A 股或美股，输入代码，保留「真实数据」，点击「开始研究」。</p><ol><li>查看七个研究角色的执行状态。</li><li>阅读真实价格趋势、数据日期与风险说明。</li><li>切换「证据来源」检查来源与快照。</li><li>点击「导出报告」保存 Markdown。</li></ol><p className="muted">真实行情为已收盘日线，非逐笔实时。新闻与宏观缺失会明确标记；美股仍可切换合成演示。当前版本为本地单用户工作区。</p><button className="primary-button" onClick={() => setHelp(false)}>开始探索 <ArrowRight size={16} /></button></section></div>}
  </div>;
}
