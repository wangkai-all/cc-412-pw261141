"""三个协作服务：

- IngestLogService  接入日志：期望/实到账目、补齐缺口（追加上游重传批次）。
- DependencyTracker 依赖追踪：口径定义与事件、镜头-数据依赖图、受影响比较计算。
- AttributionService 归因报告：把分析钉在同一输入版本上重算，管理对照缓存，
  汇总候选解释报告（候选 ≠ 已证实原因）。

缓存策略（权衡见 README）：原始证据与最终报告持久化；镜头结果按
(镜头, 输入版本, 代码版本, 参数) 缓存。修订产生新输入版本后，受影响镜头的
旧缓存键永远不会再被命中（机制上杜绝复用过期证据）；依赖未变的镜头由
依赖追踪器证明后可沿用（source_version_id 记录实际计算版本，界面明示）。
"""
import json

from .db import now_iso, sha256_json
from .lenses import CODE_VERSION, LENS_DEPS, LENS_FUNCS, LENS_KINDS, LENS_TITLES
from .versions import (basis_for_day, create_input_version, current_version_id,
                       get_manifest)

# 修订类型 -> 触碰的数据存储
REVISION_TOUCHES = {
    "backfill": {"batches", "ingest_log"},
    "retract_caliber": {"definitions"},
}


class IngestLogService:
    def __init__(self, conn):
        self.conn = conn

    def pending_gaps(self) -> list[dict]:
        """当前仍可补齐的缺口（有待重传记录的来源-日）。"""
        rows = self.conn.execute(
            "SELECT source, day, SUM(sessions) s, SUM(orders_paid) p,"
            " SUM(orders_cancelled24) c FROM pending_redeliveries"
            " GROUP BY source, day ORDER BY day, source").fetchall()
        return [dict(r) for r in rows]

    def backfill(self, source: str, day: str) -> dict | None:
        """补齐缺口：把上游重传记录追加为 redelivery 批次并登记日志（只增证据）。"""
        rows = self.conn.execute(
            "SELECT * FROM pending_redeliveries WHERE source=? AND day=?",
            (source, day)).fetchall()
        if not rows:
            return None
        ts = now_iso()
        s = sum(r["sessions"] for r in rows)
        p = sum(r["orders_paid"] for r in rows)
        c = sum(r["orders_cancelled24"] for r in rows)
        bid = f"{source}-{day}-redelivery"
        self.conn.execute(
            "INSERT INTO batches(batch_id, source, day, sessions, orders_paid,"
            " orders_cancelled24, arrived_at, kind) VALUES(?,?,?,?,?,?,?,?)",
            (bid, source, day, s, p, c, ts, "redelivery"))
        expected = self.conn.execute(
            "SELECT MAX(expected_sessions) e FROM ingest_log WHERE source=? AND day=?",
            (source, day)).fetchone()["e"]
        self.conn.execute(
            "INSERT INTO ingest_log(source, day, expected_sessions, received_sessions,"
            " status, batch_id, note, logged_at) VALUES(?,?,?,?,?,?,?,?)",
            (source, day, expected, s, "backfilled", bid, "上游重传补齐（网页操作触发）", ts))
        self.conn.execute(
            "DELETE FROM pending_redeliveries WHERE source=? AND day=?", (source, day))
        self.conn.commit()
        return {"source": source, "day": day, "sessions": s, "orders_paid": p,
                "orders_cancelled24": c}


class DependencyTracker:
    def __init__(self, conn):
        self.conn = conn

    def events(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, metric, action, day, version, ref_event_id, created_at"
            " FROM caliber_events ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def definitions(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, metric, version, spec_json FROM definition_versions ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    def retract(self, apply_event_id: int) -> dict | None:
        """撤回一次口径变更：追加 retract 事件（不修改历史）。"""
        ev = self.conn.execute(
            "SELECT * FROM caliber_events WHERE id=? AND action='apply'",
            (apply_event_id,)).fetchone()
        if not ev:
            return None
        already = self.conn.execute(
            "SELECT COUNT(*) c FROM caliber_events WHERE action='retract' AND ref_event_id=?",
            (apply_event_id,)).fetchone()["c"]
        if already:
            return None
        self.conn.execute(
            "INSERT INTO caliber_events(metric, action, day, version, ref_event_id, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (ev["metric"], "retract", ev["day"], ev["version"], apply_event_id, now_iso()))
        self.conn.commit()
        return dict(ev)

    @staticmethod
    def affected_lenses(revision_kind: str) -> list[str]:
        """依赖图：修订触碰的存储 ∩ 镜头依赖 => 受影响、必须重算的镜头。"""
        touched = REVISION_TOUCHES[revision_kind]
        return [k for k in LENS_KINDS if LENS_DEPS[k] & touched]

    @staticmethod
    def dependency_graph() -> dict:
        return {"lens_deps": {k: sorted(v) for k, v in LENS_DEPS.items()},
                "revision_touches": {k: sorted(v) for k, v in REVISION_TOUCHES.items()}}


class AttributionService:
    def __init__(self, conn):
        self.conn = conn

    # ---------------- 序列（供检测与图表） ----------------
    def daily_series(self, version_id: int, basis: str = "active") -> list[dict]:
        from .lenses import orders_under, segment_rows
        manifest = get_manifest(self.conn, version_id)
        rows = segment_rows(self.conn, manifest)
        spans = manifest["definition_spans"]
        days = sorted({d for (d, _s) in rows.keys()})
        out = []
        for day in days:
            b = basis_for_day(spans, day) if basis == "active" else basis
            sess = orders = 0
            for (d, _s), r in rows.items():
                if d == day:
                    sess += r["sessions"]
                    orders += orders_under(r, b)
            out.append({"day": day, "sessions": sess, "orders": orders,
                        "rate": (orders / sess) if sess else 0.0, "basis": b})
        return out

    # ---------------- 对照缓存 ----------------
    @staticmethod
    def lens_params(anomaly: dict) -> dict:
        return {"pre_window": anomaly["pre_window"], "post_window": anomaly["post_window"]}

    def _cache_key(self, kind: str, version_id: int, params: dict) -> str:
        return f"{kind}:{version_id}:{CODE_VERSION}:{sha256_json(params)[:16]}"

    def run_lens(self, version_id: int, anomaly: dict, kind: str) -> tuple[dict, str]:
        """跑一次镜头：命中缓存则复用，未命中则按钉住的输入版本重算。
        返回 (payload, provenance)。provenance ∈ recomputed | cache_hit | carried_forward"""
        params = self.lens_params(anomaly)
        key = self._cache_key(kind, version_id, params)
        row = self.conn.execute(
            "SELECT * FROM comparison_cache WHERE cache_key=?", (key,)).fetchone()
        if row:
            self.conn.execute(
                "UPDATE comparison_cache SET hits=hits+1 WHERE cache_key=?", (key,))
            self.conn.commit()
            prov = "cache_hit" if row["source_version_id"] == version_id else "carried_forward"
            return json.loads(row["payload_json"]), prov
        manifest = get_manifest(self.conn, version_id)
        payload = LENS_FUNCS[kind](self.conn, manifest, anomaly)
        blob = json.dumps(payload, ensure_ascii=False)
        self.conn.execute(
            "INSERT INTO comparison_cache(cache_key, kind, input_version_id,"
            " source_version_id, code_version, params_hash, payload_json, size_bytes,"
            " hits, created_at) VALUES(?,?,?,?,?,?,?,?,0,?)",
            (key, kind, version_id, version_id, CODE_VERSION,
             sha256_json(params)[:16], blob, len(blob.encode()), now_iso()))
        self.conn.commit()
        return payload, "recomputed"

    def carry_forward(self, old_version_id: int, new_version_id: int,
                      anomaly: dict, kinds: list[str]) -> list[str]:
        """依赖未变的镜头：把旧版本结果登记到新版本键下（来源版本如实记录）。"""
        carried = []
        params = self.lens_params(anomaly)
        ph = sha256_json(params)[:16]
        for kind in kinds:
            old_key = self._cache_key(kind, old_version_id, params)
            row = self.conn.execute(
                "SELECT * FROM comparison_cache WHERE cache_key=?", (old_key,)).fetchone()
            if not row:
                continue
            new_key = self._cache_key(kind, new_version_id, params)
            self.conn.execute(
                "INSERT OR IGNORE INTO comparison_cache(cache_key, kind, input_version_id,"
                " source_version_id, code_version, params_hash, payload_json, size_bytes,"
                " hits, created_at) VALUES(?,?,?,?,?,?,?,?,0,?)",
                (new_key, kind, new_version_id, row["source_version_id"], CODE_VERSION,
                 ph, row["payload_json"], row["size_bytes"], now_iso()))
            carried.append(kind)
        self.conn.commit()
        return carried

    # ---------------- 报告 ----------------
    def compose_report(self, anomaly_id: int, version_id: int, job_id: int | None,
                       provenance: dict[str, str]) -> int:
        anomaly = self.conn.execute(
            "SELECT * FROM anomalies WHERE id=?", (anomaly_id,)).fetchone()
        if not anomaly:
            raise KeyError(f"异常 {anomaly_id} 不存在")
        anomaly = dict(anomaly)
        candidates, prov_out = [], {}
        for kind in LENS_KINDS:
            payload, prov = self.run_lens(version_id, anomaly, kind)
            prov_out[kind] = prov
            payload["provenance"] = {
                "cache": provenance.get(kind, prov),
                "input_version_id": version_id,
                "code_version": CODE_VERSION,
            }
            candidates.append(payload)

        series = self.daily_series(version_id, basis="active")
        pre = anomaly["pre_window"].split("..")
        post = anomaly["post_window"].split("..")
        pre_rates = [s["rate"] for s in series if pre[0] <= s["day"] <= pre[1]]
        post_rates = [s["rate"] for s in series if post[0] <= s["day"] <= post[1]]
        mean = lambda xs: sum(xs) / len(xs) if xs else 0.0
        observed = {
            "pre_rate": round(mean(pre_rates), 5),
            "post_rate": round(mean(post_rates), 5),
            "delta_pp": round((mean(post_rates) - mean(pre_rates)) * 100, 3),
        }
        payload = {
            "anomaly": anomaly,
            "input_version_id": version_id,
            "observed": observed,
            "candidates": candidates,
            "reconciliation": {
                "note": ("三个候选各自独立估计，针对的是同一落差的不同侧面，"
                         "数值不可相加：缺口与口径的影响也会体现在结构分解的水平项里。"),
                "not_additive": True,
            },
            "disclaimer": ("以下为候选解释及其证据，均非已证实原因；"
                           "每个候选都列出了无法排除的其他可能。"),
        }
        self.conn.execute(
            "INSERT INTO reports(anomaly_id, input_version_id, job_id, created_at, payload_json)"
            " VALUES(?,?,?,?,?)",
            (anomaly_id, version_id, job_id, now_iso(),
             json.dumps(payload, ensure_ascii=False)))
        self.conn.commit()
        return self.conn.execute("SELECT last_insert_rowid() i").fetchone()["i"]

    def latest_report(self, anomaly_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM reports WHERE anomaly_id=? ORDER BY id DESC LIMIT 1",
            (anomaly_id,)).fetchone()
        if not row:
            return None
        out = json.loads(row["payload_json"])
        out["report_id"] = row["id"]
        out["created_at"] = row["created_at"]
        return out

    # ---------------- 修订入口（产生新输入版本 + 受影响比较） ----------------
    def apply_revision(self, revision_kind: str, detail: str) -> dict:
        old_vid = current_version_id(self.conn)
        new_vid = create_input_version(self.conn, revision_kind, detail)
        affected = DependencyTracker.affected_lenses(revision_kind)
        return {"old_version": old_vid, "new_version": new_vid, "affected": affected}
