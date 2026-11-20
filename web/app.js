"use strict";
/* 指标突变归因工作台前端。所有动态文本一律 textContent，不用 innerHTML 拼接。 */

const $ = (id) => document.getElementById(id);
const SVGNS = "http://www.w3.org/2000/svg";

const fmtPct = (x, d = 2) => (x * 100).toFixed(d) + "%";
const fmtPP = (x, d = 2) => (x < 0 ? "−" : "+") + Math.abs(x).toFixed(d) + " pp";
const fmtInt = (x) => Math.round(x).toLocaleString("zh-CN");
const shortHash = (h) => (h ? h.slice(0, 10) : "—");

const LENS_NAME = {
  upstream_gap: "上游数据缺口",
  caliber_change: "口径变更",
  mix_shift: "业务结构变化",
};
const STATUS_LABEL = {
  supported: "证据支持",
  weak: "弱支持",
  not_supported: "未获支持",
  retracted: "已撤回",
};
const PROV_LABEL = {
  recomputed: "本次重算",
  cache_hit: "缓存命中",
  carried_forward: "依赖未变·沿用旧版",
};
const JOB_STATUS = {
  pending: "排队中", running: "运行中", interrupted: "已中断",
  done: "已完成", failed: "失败",
};

let state = { overview: null, report: null, pollTimer: null, crossIdx: null };

/* ---------------- 主题 ---------------- */
(function initTheme() {
  const saved = localStorage.getItem("theme");
  if (saved) document.documentElement.dataset.theme = saved;
  $("themeToggle").addEventListener("click", () => {
    const cur = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = cur;
    localStorage.setItem("theme", cur);
    renderAll(); // 图表颜色来自 CSS 变量，需重绘
  });
})();

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function el(tag, attrs, ...children) {
  const node = tag === "svg" || attrs?.svg
    ? document.createElementNS(SVGNS, tag === "svg" ? "svg" : tag)
    : document.createElement(tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) {
    if (k === "svg") continue;
    if (k === "text") node.textContent = v;
    else node.setAttribute(k, v);
  }
  for (const c of children) if (c) node.appendChild(c);
  return node;
}
const txt = (s) => document.createTextNode(s);

/* ---------------- 数据 ---------------- */
async function api(path, opts) {
  const res = await fetch(path, opts);
  const body = await res.json().catch(() => ({}));
  return { status: res.status, body };
}
async function post(path, body) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}

async function refresh() {
  const ov = await api("/api/overview");
  state.overview = ov.body;
  const first = state.overview.anomalies[0];
  state.report = null;
  if (first) {
    const rep = await api(`/api/anomalies/${first.id}/report`);
    if (rep.status === 200) state.report = rep.body;
  }
  renderAll();
  schedulePoll();
}

function schedulePoll() {
  if (state.pollTimer) clearTimeout(state.pollTimer);
  const active = (state.overview.jobs || []).some(
    (j) => j.status === "running" || j.status === "pending");
  if (active) state.pollTimer = setTimeout(refresh, 800);
}

/* ---------------- 渲染入口 ---------------- */
function renderAll() {
  const ov = state.overview;
  if (!ov) return;
  $("versionBadge").textContent = `输入版本 #${ov.current_version_id}`;
  renderStats();
  renderChart();
  renderSeriesTable();
  renderReport();
  renderActions();
  renderJobs();
  renderVersions();
  renderCache();
  renderDepGraph();
}

/* ---------------- 统计块 ---------------- */
function renderStats() {
  const row = $("statRow");
  row.textContent = "";
  const rep = state.report;
  const mk = (label, value, cls) => {
    const d = el("div", { class: "stat" + (cls ? " " + cls : "") });
    d.appendChild(el("div", { class: "label", text: label }));
    d.appendChild(el("div", { class: "value", text: value }));
    return d;
  };
  if (rep) {
    row.appendChild(mk("前窗口均值（7天）", fmtPct(rep.observed.pre_rate)));
    row.appendChild(mk("后窗口均值（7天）", fmtPct(rep.observed.post_rate)));
    const hero = el("div", { class: "stat hero" });
    hero.appendChild(el("div", { class: "label", text: "落差（后 − 前）" }));
    const v = el("div", { class: "value delta-down", text: fmtPP(rep.observed.delta_pp) });
    hero.appendChild(v);
    row.appendChild(hero);
  } else {
    const s = state.overview.series;
    row.appendChild(mk("最新一日转化率", fmtPct(s[s.length - 1].rate)));
  }
}

/* ---------------- 趋势图 ---------------- */
function renderChart() {
  const ov = state.overview;
  const series = ov.series;
  const svg = $("chart");
  svg.textContent = "";
  const W = 720, H = 260, M = { l: 46, r: 14, t: 16, b: 26 };
  const iw = W - M.l - M.r, ih = H - M.t - M.b;
  const n = series.length;
  const rates = series.map((s) => s.rate);
  let lo = Math.min(...rates), hi = Math.max(...rates);
  const pad = Math.max((hi - lo) * 0.15, 0.001);
  lo -= pad; hi += pad;
  const x = (i) => M.l + (i * iw) / (n - 1);
  const y = (v) => M.t + ih - ((v - lo) / (hi - lo)) * ih;

  const cGrid = cssVar("--gridline"), cBase = cssVar("--baseline"),
    cSeries = cssVar("--series-1"), cWash = cssVar("--series-1-wash"),
    cCrit = cssVar("--status-critical"), cMuted = cssVar("--text-muted"),
    cSurface = cssVar("--surface-1");

  // 后窗口底色
  const anomaly = ov.anomalies[0];
  if (anomaly) {
    const [ps] = anomaly.post_window.split("..");
    const [pe] = anomaly.post_window.split("..").slice(-1);
    const i0 = series.findIndex((s) => s.day === ps);
    const i1 = series.findIndex((s) => s.day === pe);
    if (i0 >= 0 && i1 >= 0) {
      svg.appendChild(el("rect", {
        svg: 1, x: x(i0), y: M.t, width: x(i1) - x(i0), height: ih, fill: cWash,
      }));
      const lab = el("text", { svg: 1, x: (x(i0) + x(i1)) / 2, y: M.t + 12, "text-anchor": "middle", text: "后窗口" });
      svg.appendChild(lab);
    }
  }

  // Y 网格与刻度（实线发丝线）
  const ticks = 4;
  for (let i = 0; i <= ticks; i++) {
    const v = lo + ((hi - lo) * i) / ticks;
    const yy = y(v);
    svg.appendChild(el("line", { svg: 1, x1: M.l, x2: W - M.r, y1: yy, y2: yy, stroke: cGrid, "stroke-width": 1 }));
    svg.appendChild(el("text", { svg: 1, x: M.l - 6, y: yy + 4, "text-anchor": "end", text: (v * 100).toFixed(1) + "%" }));
  }
  // X 基线与刻度（每 7 天）
  svg.appendChild(el("line", { svg: 1, x1: M.l, x2: W - M.r, y1: H - M.b, y2: H - M.b, stroke: cBase, "stroke-width": 1 }));
  for (let i = 0; i < n; i += 7) {
    svg.appendChild(el("text", { svg: 1, x: x(i), y: H - M.b + 16, "text-anchor": "middle", text: series[i].day.slice(5) }));
  }

  // 缺口日底纹（数据注释，中性色）
  const gapDays = new Set((ov.pending_gaps || []).map((g) => g.day));
  if (state.report) {
    for (const c of state.report.candidates) {
      if (c.kind === "upstream_gap") {
        for (const g of c.evidence.gap_entries || []) gapDays.add(g.day);
      }
    }
  }
  for (let i = 0; i < n; i++) {
    if (gapDays.has(series[i].day)) {
      const w = iw / (n - 1);
      svg.appendChild(el("rect", {
        svg: 1, x: x(i) - w / 2, y: M.t, width: w, height: ih,
        fill: cBase, opacity: 0.35,
      }));
    }
  }

  // 口径事件竖线
  const events = ov.caliber_events || [];
  const retracted = new Set(events.filter((e) => e.action === "retract").map((e) => e.ref_event_id));
  for (const e of events) {
    if (e.action !== "apply") continue;
    const i = series.findIndex((s) => s.day === e.day);
    if (i < 0) continue;
    const gone = retracted.has(e.id);
    svg.appendChild(el("line", {
      svg: 1, x1: x(i), x2: x(i), y1: M.t, y2: H - M.b,
      stroke: cMuted, "stroke-width": 1, opacity: gone ? 0.4 : 0.9,
    }));
    svg.appendChild(el("text", {
      svg: 1, x: x(i) + 4, y: M.t + 24,
      text: gone ? `口径 ${e.version}（已撤回）` : `口径 ${e.version} 生效`,
    }));
  }

  // 面积淡染 + 折线
  let dLine = "", dArea = `M ${x(0)} ${y(rates[0])}`;
  series.forEach((s, i) => {
    dLine += (i ? " L " : "M ") + `${x(i)} ${y(s.rate)}`;
    if (i) dArea += ` L ${x(i)} ${y(s.rate)}`;
  });
  dArea += ` L ${x(n - 1)} ${H - M.b} L ${x(0)} ${H - M.b} Z`;
  svg.appendChild(el("path", { svg: 1, d: dArea, fill: cSeries, opacity: 0.10, stroke: "none" }));
  svg.appendChild(el("path", {
    svg: 1, d: dLine, fill: "none", stroke: cSeries, "stroke-width": 2,
    "stroke-linejoin": "round", "stroke-linecap": "round",
  }));

  // 异常日标记（图标+文字，不只靠颜色）
  if (anomaly) {
    const i = series.findIndex((s) => s.day === anomaly.day);
    if (i >= 0) {
      svg.appendChild(el("circle", {
        svg: 1, cx: x(i), cy: y(series[i].rate), r: 5,
        fill: cCrit, stroke: cSurface, "stroke-width": 2,
      }));
      svg.appendChild(el("text", { svg: 1, x: x(i) + 8, y: y(series[i].rate) - 8, text: "⚠ 异常日" }));
    }
  }
  // 末端点（≥8px，带 2px 表面色环）
  svg.appendChild(el("circle", {
    svg: 1, cx: x(n - 1), cy: y(rates[n - 1]), r: 4,
    fill: cSeries, stroke: cSurface, "stroke-width": 2,
  }));

  // 十字线层
  const cross = el("line", { svg: 1, y1: M.t, y2: H - M.b, stroke: cMuted, "stroke-width": 1, visibility: "hidden" });
  const focusDot = el("circle", { svg: 1, r: 4, fill: cSeries, stroke: cSurface, "stroke-width": 2, visibility: "hidden" });
  svg.appendChild(cross); svg.appendChild(focusDot);

  const tip = $("tooltip");
  const box = $("chartBox");
  const showAt = (i) => {
    state.crossIdx = i;
    const s = series[i];
    cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i));
    cross.setAttribute("visibility", "visible");
    focusDot.setAttribute("cx", x(i)); focusDot.setAttribute("cy", y(s.rate));
    focusDot.setAttribute("visibility", "visible");
    tip.style.display = "block";
    tip.textContent = "";
    tip.appendChild(el("div", { class: "t-val", text: fmtPct(s.rate) }));
    tip.appendChild(el("div", { class: "t-label", text: `${s.day} · 口径 ${s.basis}` }));
    tip.appendChild(el("div", { class: "t-label", text: `会话 ${fmtInt(s.sessions)} · 订单 ${fmtInt(s.orders)}` }));
    const rect = box.getBoundingClientRect();
    const px = (x(i) / 720) * rect.width;
    tip.style.left = Math.min(Math.max(px - 60, 0), rect.width - 150) + "px";
    tip.style.top = "8px";
  };
  const hide = () => {
    cross.setAttribute("visibility", "hidden");
    focusDot.setAttribute("visibility", "hidden");
    tip.style.display = "none";
  };
  box.onpointermove = (ev) => {
    const rect = box.getBoundingClientRect();
    const px = ((ev.clientX - rect.left) / rect.width) * 720;
    const i = Math.round(((px - M.l) / iw) * (n - 1));
    if (i >= 0 && i < n) showAt(i);
  };
  box.onpointerleave = hide;
  box.onkeydown = (ev) => {
    if (ev.key !== "ArrowLeft" && ev.key !== "ArrowRight") return;
    ev.preventDefault();
    const cur = state.crossIdx == null ? n - 1 : state.crossIdx;
    const next = Math.min(n - 1, Math.max(0, cur + (ev.key === "ArrowRight" ? 1 : -1)));
    showAt(next);
  };

  $("chartSub").textContent =
    `区间 ${series[0].day} ~ ${series[n - 1].day} · 阴影为后窗口与缺口日 · 悬停或方向键查看每日数值`;
}

function renderSeriesTable() {
  const t = $("seriesTable");
  t.textContent = "";
  const gapDays = new Set((state.overview.pending_gaps || []).map((g) => g.day));
  const head = el("tr");
  for (const h of ["日期", "会话", "订单", "转化率", "口径", "标记"]) {
    head.appendChild(el("th", { class: h === "日期" || h === "口径" || h === "标记" ? "" : "num", text: h }));
  }
  t.appendChild(head);
  for (const s of state.overview.series) {
    const tr = el("tr");
    tr.appendChild(el("td", { text: s.day }));
    tr.appendChild(el("td", { class: "num", text: fmtInt(s.sessions) }));
    tr.appendChild(el("td", { class: "num", text: fmtInt(s.orders) }));
    tr.appendChild(el("td", { class: "num", text: fmtPct(s.rate) }));
    tr.appendChild(el("td", { text: s.basis }));
    tr.appendChild(el("td", { text: gapDays.has(s.day) ? "缺口" : "" }));
    t.appendChild(tr);
  }
}

/* ---------------- 报告 ---------------- */
function renderReport() {
  const rep = state.report;
  const meta = $("reportMeta"), obs = $("observedRow"),
    sum = $("impactSummary"), box = $("candidates"), rec = $("reconcileNote");
  meta.textContent = ""; obs.textContent = ""; sum.textContent = "";
  box.textContent = ""; rec.textContent = "";
  if (!rep) {
    meta.textContent = "尚无归因报告。可点击下方「重跑受影响比较」生成。";
    return;
  }
  const a = rep.anomaly;
  meta.textContent =
    `异常日 ${a.day} · 前窗口 ${a.pre_window} · 后窗口 ${a.post_window} · ` +
    `报告 #${rep.report_id} · 输入版本 #${rep.input_version_id} · 生成于 ${rep.created_at}`;

  // 影响量级条（量级任务 → 单色调；数值在条端）
  const title = el("div", { class: "note", text: "各候选对落差的解释量级（独立估计，方向均为压低指标）：" });
  sum.appendChild(title);
  const bars = el("div", { class: "impact-bars" });
  const explained = rep.candidates.map((c) => ({
    name: c.title,
    pp: c.kind === "upstream_gap" ? c.estimated_impact_pp : -c.estimated_impact_pp,
  }));
  const maxAbs = Math.max(...explained.map((e) => Math.abs(e.pp)), 0.01);
  for (const e of explained) {
    const row = el("div", { class: "impact-bar-row" });
    row.appendChild(el("span", { class: "name", text: e.name }));
    const track = el("div", { class: "track" });
    const w = Math.max((Math.abs(e.pp) / maxAbs) * 100, 1);
    track.appendChild(el("div", { class: "fill", style: `width:${w}%` }));
    row.appendChild(track);
    row.appendChild(el("span", { class: "val", text: `≈ ${e.pp.toFixed(2)} pp` }));
    bars.appendChild(row);
  }
  sum.appendChild(bars);
  rec.textContent = rep.reconciliation.note + " " + rep.disclaimer;

  for (const c of rep.candidates) box.appendChild(renderCandidate(c));
}

function renderCandidate(c) {
  const card = el("div", { class: "candidate" });
  const h = el("h3");
  h.appendChild(txt(c.title));
  const badge = el("span", { class: "badge" + (c.status === "supported" ? " supported" : "") });
  badge.textContent = STATUS_LABEL[c.status] || c.status;
  h.appendChild(badge);
  const prov = c.provenance || {};
  h.appendChild(el("span", {
    class: "meta",
    text: `${PROV_LABEL[prov.cache] || prov.cache} · 输入版 #${prov.input_version_id} · 代码 v${prov.code_version}`,
  }));
  card.appendChild(h);

  const impact = el("div", { class: "impact-line" });
  impact.appendChild(txt("估计影响（候选，未证实）："));
  impact.appendChild(el("b", { text: fmtPP(c.estimated_impact_pp) }));
  impact.appendChild(txt(`　${c.impact_direction || ""}`));
  card.appendChild(impact);

  card.appendChild(el("div", { class: "assumptions", text: "方法：" + c.method }));
  card.appendChild(el("div", {
    class: "assumptions",
    text: "前提假设：" + (c.assumptions || []).join("；"),
  }));

  const det = el("details");
  det.appendChild(el("summary", { text: "展开证据" }));
  det.appendChild(renderEvidence(c));
  card.appendChild(det);

  card.appendChild(el("h4", { text: "无法排除的可能" }));
  const ul = el("ul", { class: "unexcluded" });
  for (const u of c.unexcluded || []) ul.appendChild(el("li", { text: u }));
  card.appendChild(ul);
  return card;
}

function tableFrom(headers, rows) {
  const t = el("table");
  const tr = el("tr");
  for (const [label, num] of headers) tr.appendChild(el("th", { class: num ? "num" : "", text: label }));
  t.appendChild(tr);
  for (const r of rows) {
    const tr2 = el("tr");
    r.forEach((cell, i) => tr2.appendChild(el("td", { class: headers[i][1] ? "num" : "", text: String(cell) })));
    t.appendChild(tr2);
  }
  return t;
}

function renderEvidence(c) {
  const wrap = el("div");
  const ev = c.evidence || {};
  if (c.kind === "upstream_gap") {
    wrap.appendChild(el("h4", { text: "缺口明细（接入日志：期望 vs 实到）" }));
    wrap.appendChild(tableFrom(
      [["日期"], ["来源"], ["期望会话", 1], ["实到会话", 1], ["缺失", 1], ["日志状态"]],
      (ev.gap_entries || []).map((g) => [g.day, g.source, fmtInt(g.expected), fmtInt(g.received), fmtInt(g.missing), g.statuses]),
    ));
    wrap.appendChild(el("div", {
      class: "note", text:
        `缺失会话合计 ${fmtInt(ev.missing_sessions_total)}（占窗口 ${(ev.missing_share_of_window * 100).toFixed(1)}%）· ` +
        `估计缺失订单 ${ev.est_missing_orders} · 后窗口实际 ${fmtPct(ev.post_rate_v1)} → 若补齐 ${fmtPct(ev.counterfactual_rate_v1)}（v1 口径）`,
    }));
  } else if (c.kind === "caliber_change") {
    const defs = ev.definitions || {};
    wrap.appendChild(el("h4", { text: "口径定义差异" }));
    wrap.appendChild(tableFrom(
      [["版本"], ["定义"]],
      Object.entries(defs).map(([v, spec]) => {
        let label = v;
        try { label = `${v}：${JSON.parse(spec).label}`; } catch (_) { /* 保留原文 */ }
        return [v, label];
      }),
    ));
    wrap.appendChild(el("h4", { text: "双跑对照（同一输入版本，v1 vs v2）" }));
    wrap.appendChild(tableFrom(
      [["日期"], ["v1 转化率", 1], ["v2 转化率", 1], ["差值 pp", 1]],
      (ev.dual_run_post || []).map((d) => [d.day, fmtPct(d.rate_v1), fmtPct(d.rate_v2), fmtPP(d.delta_pp)]),
    ));
    wrap.appendChild(el("div", {
      class: "note", text:
        `后窗口平均差值 ${fmtPP(ev.dual_run_post_mean_delta_pp)} · 前窗口平均差值 ${fmtPP(ev.dual_run_pre_mean_delta_pp)}（前窗口两口径应接近，作为对照 sanity check）`,
    }));
  } else if (c.kind === "mix_shift") {
    for (const [key, label] of [["raw_view", "原始视图（生效口径）"], ["adjusted_view", "调整后视图（剔除已估计缺口与口径影响）"]]) {
      const v = ev[key];
      if (!v) continue;
      wrap.appendChild(el("h4", { text: label }));
      const rows = v.segments.map((s) => [
        s.source, fmtPct(s.w0, 1), fmtPct(s.w1, 1), fmtPct(s.r0), fmtPct(s.r1),
        fmtPP(s.within_pp), fmtPP(s.mix_pp), fmtPP(s.interaction_pp),
      ]);
      rows.push(["合计", "", "", "", "", fmtPP(v.within_pp), fmtPP(v.mix_pp), fmtPP(v.interaction_pp)]);
      wrap.appendChild(tableFrom(
        [["来源"], ["前占比", 1], ["后占比", 1], ["前转化率", 1], ["后转化率", 1],
         ["水平项 pp", 1], ["结构项 pp", 1], ["交互项 pp", 1]],
        rows,
      ));
      wrap.appendChild(el("div", {
        class: "note",
        text: `三项合计 ${fmtPP(v.within_pp + v.mix_pp + v.interaction_pp)} 与实测落差 ${fmtPP(v.delta_pp)} 的恒等式残差 ${v.identity_residual.toExponential(1)}（应≈0，可复核）`,
      }));
    }
    wrap.appendChild(el("div", { class: "note", text: ev.adjusted_note || "" }));
  }
  return wrap;
}

/* ---------------- 操作区 ---------------- */
function renderActions() {
  const ov = state.overview;
  const ga = $("gapActions");
  ga.textContent = "";
  if (!ov.pending_gaps.length) {
    ga.appendChild(el("div", { class: "note", text: "当前没有待补齐的缺口。" }));
  } else {
    for (const g of ov.pending_gaps) {
      const row = el("div", { style: "margin:4px 0" });
      row.appendChild(txt(`${g.day} · ${g.source} · 缺失约 ${fmtInt(g.s)} 会话 `));
      const btn = el("button", { type: "button", text: "补齐" });
      btn.onclick = () => act(() => post("/api/gaps/backfill", { source: g.source, day: g.day }));
      row.appendChild(btn);
      ga.appendChild(row);
    }
    const all = el("button", { type: "button", text: "全部补齐", class: "primary" });
    all.onclick = () => act(() => post("/api/gaps/backfill", { all: true }));
    ga.appendChild(el("div", { style: "margin-top:6px" }, all));
  }

  const ca = $("caliberActions");
  ca.textContent = "";
  const events = ov.caliber_events || [];
  const retracted = new Set(events.filter((e) => e.action === "retract").map((e) => e.ref_event_id));
  const applies = events.filter((e) => e.action === "apply");
  if (!applies.length) ca.appendChild(el("div", { class: "note", text: "暂无口径变更事件。" }));
  for (const e of applies) {
    const row = el("div", { style: "margin:4px 0" });
    const gone = retracted.has(e.id);
    row.appendChild(txt(`${e.day} 起生效 ${e.version}（${gone ? "已撤回" : "生效中"}） `));
    if (!gone) {
      const btn = el("button", { type: "button", text: "撤回此次变更", class: "danger" });
      btn.onclick = () => act(() => post("/api/caliber/retract", { event_id: e.id }));
      row.appendChild(btn);
    }
    ca.appendChild(row);
  }

  const anomaly = ov.anomalies[0];
  $("rerunBtn").disabled = !anomaly;
  $("rerunBtn").onclick = () => act(() => post(`/api/anomalies/${anomaly.id}/rerun`, {}));
}

async function act(fn) {
  const { status, body } = await fn();
  let msg;
  if (status >= 400) {
    msg = body.error || "操作失败";
  } else {
    const ver = body.new_version != null
      ? `新输入版本 #${body.new_version}`
      : `输入版本 #${body.input_version_id ?? state.overview.current_version_id}（未变）`;
    msg = `${body.revision || "已触发"} → ${ver}` +
      (body.affected_lenses ? `，受影响比较：${body.affected_lenses.map((k) => LENS_NAME[k]).join("、")}` : "") +
      (body.carried_forward && body.carried_forward.length
        ? `；依赖未变沿用：${body.carried_forward.map((k) => LENS_NAME[k]).join("、")}` : "");
  }
  $("rerunNote").textContent = msg;
  await refresh();
}

/* ---------------- 任务 ---------------- */
function renderJobs() {
  const box = $("jobs");
  box.textContent = "";
  const jobs = (state.overview.jobs || []).slice(0, 4);
  if (!jobs.length) return;
  box.appendChild(el("h4", { text: "分析任务" }));
  for (const j of jobs) {
    const card = el("div", { class: "job" });
    const head = el("div");
    head.appendChild(el("span", {
      class: "status-text",
      text: `任务 #${j.id} · 输入版本 #${j.input_version_id} · ${JOB_STATUS[j.status] || j.status}`,
    }));
    card.appendChild(head);
    const steps = el("div", { class: "steps" });
    for (const s of j.steps) {
      const chip = el("span", { class: "step " + s.status });
      let label = s.label;
      if (s.status === "done" && s.detail && s.detail.provenance) {
        label += `（${PROV_LABEL[s.detail.provenance] || s.detail.provenance}）`;
      }
      chip.textContent = (s.status === "done" ? "✓ " : s.status === "running" ? "… " : "○ ") + label;
      steps.appendChild(chip);
    }
    card.appendChild(steps);
    const actions = el("div", { class: "actions" });
    if (j.status === "running" || j.status === "pending") {
      const b = el("button", { type: "button", text: "中断" });
      b.onclick = () => post(`/api/jobs/${j.id}/interrupt`).then(refresh);
      actions.appendChild(b);
    }
    if (j.status === "interrupted" || j.status === "failed") {
      const b = el("button", { type: "button", text: "继续（从断点）", class: "primary" });
      b.onclick = () => post(`/api/jobs/${j.id}/resume`).then(refresh);
      actions.appendChild(b);
      if (j.error) actions.appendChild(el("span", { class: "note", text: "错误：" + j.error }));
    }
    card.appendChild(actions);
    box.appendChild(card);
  }
}

/* ---------------- 版本 / 缓存 / 依赖 ---------------- */
const REVISION_LABEL = { initial: "初始装载", backfill: "补齐缺口", retract_caliber: "撤回口径" };

function renderVersions() {
  const t = $("versionsTable");
  t.textContent = "";
  const head = el("tr");
  for (const h of ["#", "时间", "原因", "哈希", "状态"]) head.appendChild(el("th", { text: h }));
  t.appendChild(head);
  const cur = state.overview.current_version_id;
  for (const v of state.overview.versions) {
    const tr = el("tr");
    tr.appendChild(el("td", { text: String(v.id) }));
    tr.appendChild(el("td", { text: v.created_at.replace("T", " ").slice(0, 19) }));
    tr.appendChild(el("td", { text: (REVISION_LABEL[v.reason] || v.reason) + (v.detail ? `：${v.detail}` : "") }));
    tr.appendChild(el("td", { class: "mono", text: shortHash(v.manifest_hash) }));
    tr.appendChild(el("td", { text: v.id === cur ? "当前" : `已被 #${v.superseded_by} 取代` }));
    t.appendChild(tr);
  }
}

function renderCache() {
  const t = $("cacheTable");
  t.textContent = "";
  const head = el("tr");
  for (const h of ["镜头", "输入版", "计算于", "命中", "大小", "状态"]) head.appendChild(el("th", { text: h }));
  t.appendChild(head);
  const cur = state.overview.current_version_id;
  for (const c of state.overview.cache) {
    const tr = el("tr");
    tr.appendChild(el("td", { text: LENS_NAME[c.kind] || c.kind }));
    tr.appendChild(el("td", { class: "num", text: `#${c.input_version_id}` }));
    tr.appendChild(el("td", { class: "num", text: `#${c.source_version_id}` }));
    tr.appendChild(el("td", { class: "num", text: String(c.hits) }));
    tr.appendChild(el("td", { class: "num", text: (c.size_bytes / 1024).toFixed(1) + " KB" }));
    tr.appendChild(el("td", {
      text: c.input_version_id === cur
        ? (c.source_version_id === cur ? "当前·可命中" : "当前·沿用旧版")
        : "历史·不可命中",
    }));
    t.appendChild(tr);
  }
}

function renderDepGraph() {
  const g = state.overview.dependency_graph;
  const box = $("depGraph");
  box.textContent = "";
  const lines = [];
  for (const [lens, deps] of Object.entries(g.lens_deps || {})) {
    lines.push(`${LENS_NAME[lens]} ← 依赖 [${deps.join(", ")}]`);
  }
  lines.push("修订触碰：补齐缺口 → [batches, ingest_log]；撤回口径 → [definitions]");
  lines.push("受影响比较 = 镜头依赖 ∩ 修订触碰；未受影响的镜头经依赖追踪器证明后沿用，来源版本如实记录。");
  for (const l of lines) box.appendChild(el("div", { text: l }));
}

/* ---------------- 启动 ---------------- */
refresh();
