"""三个分析镜头：上游数据缺口 / 口径变更 / 业务结构变化。

每个镜头只输出「候选解释 + 证据 + 无法排除的可能」，输出的是估计与假设，
从不把候选说成已证实原因。所有计算确定性、可复核（分解恒等式闭合校验）。
"""
from .detect import window_days
from .versions import basis_for_day

CODE_VERSION = "1.0.0"

LENS_KINDS = ("upstream_gap", "caliber_change", "mix_shift")

LENS_TITLES = {
    "upstream_gap": "上游数据缺口",
    "caliber_change": "口径变更",
    "mix_shift": "业务结构变化",
}

# 每个镜头依赖的数据存储 —— 依赖追踪器用它计算「受影响比较」
LENS_DEPS = {
    "upstream_gap": {"batches", "ingest_log"},
    "caliber_change": {"batches", "definitions"},
    "mix_shift": {"batches", "definitions"},
}


# ---------------------------------------------------------------- 公共取数

def segment_rows(conn, manifest) -> dict:
    """按清单中的批次集合聚合出 (day, source) -> 会话/订单。批次表只增，故历史版本可重放。"""
    ids = manifest["batch_ids"]
    out = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        q = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT source, day, SUM(sessions) s, SUM(orders_paid) p,"
            f" SUM(orders_cancelled24) c FROM batches WHERE batch_id IN ({q})"
            f" GROUP BY source, day", chunk).fetchall()
        for r in rows:
            out[(r["day"], r["source"])] = {
                "sessions": r["s"], "orders_paid": r["p"], "orders_cancelled24": r["c"]}
    return out


def orders_under(row: dict, basis: str) -> int:
    if basis == "v2":
        return row["orders_paid"] - row["orders_cancelled24"]
    return row["orders_paid"]


def window_aggregate(rows: dict, days: list[str], basis: str) -> dict:
    """窗口内按来源聚合：{source: {sessions, orders}} 与总计。"""
    per_src: dict[str, dict] = {}
    for (day, src), r in rows.items():
        if day not in days:
            continue
        acc = per_src.setdefault(src, {"sessions": 0, "orders": 0})
        acc["sessions"] += r["sessions"]
        acc["orders"] += orders_under(r, basis)
    total_s = sum(a["sessions"] for a in per_src.values())
    total_o = sum(a["orders"] for a in per_src.values())
    return {"per_source": per_src, "sessions": total_s, "orders": total_o,
            "rate": (total_o / total_s) if total_s else 0.0}


def ingest_gaps(conn, manifest, days: list[str]) -> list[dict]:
    """接入日志（按清单水位截断）在指定日期内的缺口：期望 vs 实到（同一来源一天可能多条日志）。"""
    max_id = manifest["ingest_log_max_id"]
    q = ",".join("?" * len(days))
    rows = conn.execute(
        f"SELECT source, day, MAX(expected_sessions) expected, SUM(received_sessions) received,"
        f" GROUP_CONCAT(status, ',') statuses, GROUP_CONCAT(COALESCE(note,''), '；') notes"
        f" FROM ingest_log WHERE id <= ? AND day IN ({q}) GROUP BY source, day"
        f" ORDER BY day, source", [max_id, *days]).fetchall()
    gaps = []
    for r in rows:
        if r["received"] < r["expected"] * 0.98:
            gaps.append({"source": r["source"], "day": r["day"],
                         "expected": r["expected"], "received": r["received"],
                         "missing": r["expected"] - r["received"],
                         "statuses": r["statuses"], "notes": r["notes"]})
    return gaps


# ---------------------------------------------------------------- 镜头一：上游数据缺口

def lens_upstream_gap(conn, manifest, anomaly) -> dict:
    rows = segment_rows(conn, manifest)
    pre_days = window_days(anomaly["pre_window"])
    post_days = window_days(anomaly["post_window"])
    gaps = ingest_gaps(conn, manifest, pre_days + post_days)
    post_gaps = [g for g in gaps if g["day"] in post_days]

    # 缺口估计固定在 v1 原始口径（完整性问题与口径定义无关），因此本镜头不依赖 definitions
    post_v1 = window_aggregate(rows, post_days, "v1")
    pre_v1 = window_aggregate(rows, pre_days, "v1")

    # 每个缺口来源的参考转化率：同来源、后窗口内非缺口日（不足则取前窗口）的 v1 转化率
    gap_sources = sorted({g["source"] for g in post_gaps})
    gap_day_set = {(g["source"], g["day"]) for g in post_gaps}
    ref_conv = {}
    for src in gap_sources:
        num = den = 0
        for (day, s), r in rows.items():
            if s == src and day in post_days and (s, day) not in gap_day_set:
                num += orders_under(r, "v1")
                den += r["sessions"]
        if den == 0:
            for (day, s), r in rows.items():
                if s == src and day in pre_days:
                    num += orders_under(r, "v1")
                    den += r["sessions"]
        ref_conv[src] = (num / den) if den else 0.0

    miss_sess = sum(g["missing"] for g in post_gaps)
    miss_orders = sum(g["missing"] * ref_conv[g["source"]] for g in post_gaps)
    cf_sessions = post_v1["sessions"] + miss_sess
    cf_orders = post_v1["orders"] + miss_orders
    cf_rate = cf_orders / cf_sessions if cf_sessions else 0.0
    impact_pp = (cf_rate - post_v1["rate"]) * 100  # 缺口压低了多少（百分点，正数=有压低）

    missing_share = miss_sess / cf_sessions if cf_sessions else 0.0
    if impact_pp >= 0.10 and missing_share >= 0.01:
        status = "supported"
    elif impact_pp >= 0.03:
        status = "weak"
    else:
        status = "not_supported"

    return {
        "kind": "upstream_gap",
        "title": LENS_TITLES["upstream_gap"],
        "status": status,
        "estimated_impact_pp": round(impact_pp, 3),
        "impact_direction": "缺口压低了指标" if impact_pp > 0 else "缺口抬高了指标",
        "method": ("接入日志的期望 vs 实到估计缺失会话；按同来源、同窗口已送达记录的转化率"
                   "（v1 原始口径，完整性估计与口径定义无关）折算缺失订单，构造「若补齐」反事实，"
                   "与后窗口实际值比较。"),
        "assumptions": ["缺失记录与同来源同窗口已送达记录同分布（转化率相同）",
                        "上游投递清单中的期望量准确"],
        "evidence": {
            "gap_entries": post_gaps,
            "pre_window_gaps": [g for g in gaps if g["day"] in pre_days],
            "missing_sessions_total": miss_sess,
            "missing_share_of_window": round(missing_share, 4),
            "ref_conv_v1": {k: round(v, 4) for k, v in ref_conv.items()},
            "est_missing_orders": round(miss_orders, 1),
            "post_rate_v1": round(post_v1["rate"], 5),
            "counterfactual_rate_v1": round(cf_rate, 5),
            "pre_rate_v1": round(pre_v1["rate"], 5),
        },
        "unexcluded": [
            "缺口窗口内真实需求可能同时发生变化——缺口只能解释「记录缺失」这一部分",
            "「缺失记录与已送达记录同分布」的假设无法用现有数据验证",
            "期望量来自上游投递清单，若清单本身不准，缺口估计随之偏差",
            "其他来源可能存在未被清单覆盖的隐性缺口",
        ],
    }


# ---------------------------------------------------------------- 镜头二：口径变更

def lens_caliber_change(conn, manifest, anomaly) -> dict:
    rows = segment_rows(conn, manifest)
    pre_days = window_days(anomaly["pre_window"])
    post_days = window_days(anomaly["post_window"])
    all_days = pre_days + post_days

    max_ev = manifest["caliber_event_max_id"]
    events = [dict(r) for r in conn.execute(
        "SELECT id, action, day, version, ref_event_id FROM caliber_events"
        " WHERE id <= ? ORDER BY id", (max_ev,)).fetchall()]
    applies = [e for e in events if e["action"] == "apply"]
    retracted_ids = {e["ref_event_id"] for e in events if e["action"] == "retract"}
    post_start, post_end = post_days[0], post_days[-1]

    # 与后窗口相邻（前3天至窗口末）的口径事件
    from datetime import date, timedelta
    lo = (date.fromisoformat(post_start) - timedelta(days=3)).isoformat()
    nearby = [a for a in applies if lo <= a["day"] <= post_end]

    defs = {r["version"]: r["spec_json"] for r in conn.execute(
        "SELECT version, spec_json FROM definition_versions").fetchall()}

    # 双跑：同一输入版本上分别按 v1 / v2 重算
    dual = []
    for day in all_days:
        agg = window_aggregate(rows, [day], "v1")
        agg2 = window_aggregate(rows, [day], "v2")
        dual.append({"day": day, "rate_v1": round(agg["rate"], 5),
                     "rate_v2": round(agg2["rate"], 5),
                     "delta_pp": round((agg2["rate"] - agg["rate"]) * 100, 3)})
    post_dual = [d for d in dual if d["day"] in post_days]
    pre_dual = [d for d in dual if d["day"] in pre_days]
    mean = lambda xs: sum(xs) / len(xs) if xs else 0.0
    post_delta = mean([d["delta_pp"] for d in post_dual])
    pre_delta = mean([d["delta_pp"] for d in pre_dual])

    active_nearby = [a for a in nearby if a["id"] not in retracted_ids]
    if not nearby:
        status, note = "not_supported", "异常窗口附近没有口径事件"
    elif not active_nearby:
        status, note = "retracted", "口径事件已被撤回，当前序列不再包含其影响（双跑结果保留供审计）"
    elif abs(post_delta) >= 0.05:
        status, note = "supported", "存在生效中的口径变更，且双跑差值不可忽略"
    else:
        status, note = "weak", "存在口径事件，但双跑差值很小"

    return {
        "kind": "caliber_change",
        "title": LENS_TITLES["caliber_change"],
        "status": status,
        "status_note": note,
        "estimated_impact_pp": round(post_delta, 3) if active_nearby else 0.0,
        "impact_direction": "口径变更压低了指标" if post_delta < 0 else "口径变更抬高了指标",
        "method": ("双跑：在同一输入版本上分别按 v1 / v2 口径重算每日指标，"
                   "差值即「定义本身」的影响，与数据是否缺失无关。"),
        "assumptions": ["双跑覆盖的口径差异已完整记录在定义版本中（无未登记的口径漂移）"],
        "evidence": {
            "definitions": defs,
            "events_nearby": nearby,
            "retracted_event_ids": sorted(retracted_ids),
            "active_spans": manifest["definition_spans"],
            "dual_run_post": post_dual,
            "dual_run_pre_mean_delta_pp": round(pre_delta, 3),
            "dual_run_post_mean_delta_pp": round(post_delta, 3),
        },
        "unexcluded": [
            "双跑只能量化「定义本身」的影响；切换当天若叠加真实业务变化，无法借此分离",
            "历史数据按旧口径记录，旧口径本身的偏差不在本分析范围内",
            "撤回口径变更不等于证明其没有影响，只是当前序列不再包含它",
        ],
    }


# ---------------------------------------------------------------- 镜头三：业务结构变化

def _bennet(agg0: dict, agg1: dict) -> dict:
    """精确三项分解：ΔR = Σw0·Δr（水平，前窗口权重）+ Σr0·Δw（结构，前窗口转化率）
    + ΣΔw·Δr（交互）。逐项展开可得 w1r1 − w0r0，恒等闭合、可复核。"""
    srcs = sorted(set(agg0["per_source"]) | set(agg1["per_source"]))
    tot_s0 = agg0["sessions"] or 1
    tot_s1 = agg1["sessions"] or 1
    rows = []
    t_within = t_mix = t_inter = 0.0
    for s in srcs:
        a0 = agg0["per_source"].get(s, {"sessions": 0, "orders": 0})
        a1 = agg1["per_source"].get(s, {"sessions": 0, "orders": 0})
        w0 = a0["sessions"] / tot_s0
        w1 = a1["sessions"] / tot_s1
        r0 = a0["orders"] / a0["sessions"] if a0["sessions"] else 0.0
        r1 = a1["orders"] / a1["sessions"] if a1["sessions"] else 0.0
        dw, dr = w1 - w0, r1 - r0
        within, mix, inter = w0 * dr, r0 * dw, dw * dr
        t_within += within
        t_mix += mix
        t_inter += inter
        rows.append({"source": s, "w0": round(w0, 4), "w1": round(w1, 4),
                     "r0": round(r0, 5), "r1": round(r1, 5),
                     "within_pp": round(within * 100, 3),
                     "mix_pp": round(mix * 100, 3),
                     "interaction_pp": round(inter * 100, 3)})
    delta_r = agg1["rate"] - agg0["rate"]
    total = t_within + t_mix + t_inter
    return {
        "segments": rows,
        "within_pp": round(t_within * 100, 3),
        "mix_pp": round(t_mix * 100, 3),
        "interaction_pp": round(t_inter * 100, 3),
        "delta_pp": round(delta_r * 100, 3),
        "identity_residual": round(delta_r - total, 12),  # 恒等式校验：应≈0
    }


def lens_mix_shift(conn, manifest, anomaly) -> dict:
    rows = segment_rows(conn, manifest)
    spans = manifest["definition_spans"]
    pre_days = window_days(anomaly["pre_window"])
    post_days = window_days(anomaly["post_window"])

    # 原始视图：按生效口径（active）聚合前后窗口
    def active_agg(days):
        per_src: dict[str, dict] = {}
        for day in days:
            basis = basis_for_day(spans, day)
            for (d, src), r in rows.items():
                if d != day:
                    continue
                acc = per_src.setdefault(src, {"sessions": 0, "orders": 0})
                acc["sessions"] += r["sessions"]
                acc["orders"] += orders_under(r, basis)
        ts = sum(a["sessions"] for a in per_src.values())
        to = sum(a["orders"] for a in per_src.values())
        return {"per_source": per_src, "sessions": ts, "orders": to,
                "rate": (to / ts) if ts else 0.0}

    raw = _bennet(active_agg(pre_days), active_agg(post_days))

    # 调整后视图：v1 口径 + 补齐估计缺口（剔除缺口与口径两类已估计影响后的剩余结构）
    gaps = ingest_gaps(conn, manifest, post_days)
    gap_missing = {(g["source"], g["day"]): g["missing"] for g in gaps}
    # 缺失订单按缺口镜头的同分布假设折算
    ref = {}
    for (src, _day), miss in gap_missing.items():
        if src in ref:
            continue
        num = den = 0
        for (day, s), r in rows.items():
            if s == src and day in post_days and (s, day) not in gap_missing:
                num += orders_under(r, "v1")
                den += r["sessions"]
        ref[src] = (num / den) if den else 0.0

    def adjusted_agg(days, fill_gaps: bool):
        per_src: dict[str, dict] = {}
        for day in days:
            for (d, src), r in rows.items():
                if d != day:
                    continue
                acc = per_src.setdefault(src, {"sessions": 0, "orders": 0})
                sess = r["sessions"]
                orders = orders_under(r, "v1")
                miss = gap_missing.get((src, day), 0) if fill_gaps else 0
                acc["sessions"] += sess + miss
                acc["orders"] += orders + miss * ref.get(src, 0.0)
        ts = sum(a["sessions"] for a in per_src.values())
        to = sum(a["orders"] for a in per_src.values())
        return {"per_source": per_src, "sessions": ts, "orders": to,
                "rate": (to / ts) if ts else 0.0}

    adjusted = _bennet(adjusted_agg(pre_days, fill_gaps=False),
                       adjusted_agg(post_days, fill_gaps=True))

    mix_pp = adjusted["mix_pp"]
    if abs(mix_pp) >= 0.10:
        status = "supported"
    elif abs(mix_pp) >= 0.03:
        status = "weak"
    else:
        status = "not_supported"

    return {
        "kind": "mix_shift",
        "title": LENS_TITLES["mix_shift"],
        "status": status,
        "estimated_impact_pp": round(mix_pp, 3),
        "impact_direction": "结构变化压低了指标" if mix_pp < 0 else "结构变化抬高了指标",
        "method": ("精确三项分解：ΔR = Σw0·Δr（各来源水平变化，前窗口权重）"
                   "+ Σr0·Δw（来源结构变化，前窗口转化率）+ ΣΔw·Δr（交互），"
                   "恒等式闭合、每步可复核。原始视图按生效口径；"
                   "调整后视图先剔除已估计的缺口与口径影响，再看剩余结构。"),
        "assumptions": ["渠道三分的粒度足以刻画结构变化",
                        "调整后视图中的缺口折算沿用缺口镜头的同分布假设"],
        "evidence": {
            "raw_view": raw,
            "adjusted_view": adjusted,
            "adjusted_note": "调整后视图：v1 口径 + 补齐估计缺口；残余即未被缺口/口径解释的部分",
        },
        "unexcluded": [
            "结构变化背后的驱动（促销、季节、竞争、产品改动）不在本数据内，无法确认",
            "渠道粒度可能过粗，更细维度（地区/客群）可能呈现不同结构",
            "前后窗口的选择会影响分解结果",
            "交互项无法唯一归因到「结构」或「水平」",
        ],
    }


LENS_FUNCS = {
    "upstream_gap": lens_upstream_gap,
    "caliber_change": lens_caliber_change,
    "mix_shift": lens_mix_shift,
}
