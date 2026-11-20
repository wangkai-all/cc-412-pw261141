"""输入版本：把「批次集合 + 接入日志水位 + 口径事件水位」钉成一份清单。

所有底层证据表都是只增的，因此任何历史输入版本都可以精确重放；
修订（补齐/撤回）只会追加证据并产生新版本，绝不会让旧版本的缓存键再被命中。
"""
import json

from .db import now_iso, sha256_json
from .seed import METRIC


def resolve_definitions(events: list[dict]) -> list[dict]:
    """把口径事件日志折叠成生效时间线：[{version, from_day}]，按 from_day 升序。
    v1 为基底口径；apply 追加一段生效区间；retract 撤销对应的 apply。"""
    applies: list[dict] = []
    for ev in sorted(events, key=lambda e: e["id"]):
        if ev["action"] == "apply":
            applies.append({"event_id": ev["id"], "version": ev["version"], "from_day": ev["day"]})
        elif ev["action"] == "retract":
            applies = [a for a in applies if a["event_id"] != ev["ref_event_id"]]
    spans = [{"version": "v1", "from_day": "0000-01-01", "event_id": None}]
    spans += sorted(applies, key=lambda a: a["from_day"])
    return spans


def basis_for_day(spans: list[dict], day: str) -> str:
    """某一天在生效口径时间线下应使用的口径版本。"""
    chosen = "v1"
    for s in spans:
        if s["from_day"] <= day:
            chosen = s["version"]
    return chosen


def build_manifest(conn) -> dict:
    batch_ids = [r["batch_id"] for r in conn.execute("SELECT batch_id FROM batches ORDER BY batch_id")]
    ingest_max = conn.execute("SELECT COALESCE(MAX(id),0) m FROM ingest_log").fetchone()["m"]
    events = [dict(r) for r in conn.execute(
        "SELECT id, metric, action, day, version, ref_event_id FROM caliber_events ORDER BY id")]
    ev_max = events[-1]["id"] if events else 0
    spans = resolve_definitions(events)
    return {
        "metric": METRIC,
        "batch_ids": batch_ids,
        "ingest_log_max_id": ingest_max,
        "caliber_event_max_id": ev_max,
        "definition_spans": spans,
    }


def create_input_version(conn, reason: str, detail: str) -> int:
    manifest = build_manifest(conn)
    h = sha256_json(manifest)
    cur = conn.execute(
        "SELECT id FROM input_versions WHERE superseded_by IS NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.execute(
        "INSERT INTO input_versions(created_at, reason, detail, manifest_json, manifest_hash)"
        " VALUES(?,?,?,?,?)",
        (now_iso(), reason, detail, json.dumps(manifest, ensure_ascii=False), h),
    )
    new_id = conn.execute("SELECT last_insert_rowid() i").fetchone()["i"]
    if cur:
        conn.execute("UPDATE input_versions SET superseded_by=? WHERE id=?", (new_id, cur["id"]))
    conn.commit()
    return new_id


def current_version_id(conn) -> int:
    row = conn.execute(
        "SELECT id FROM input_versions WHERE superseded_by IS NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row["id"]


def get_manifest(conn, version_id: int) -> dict:
    row = conn.execute("SELECT manifest_json FROM input_versions WHERE id=?", (version_id,)).fetchone()
    if not row:
        raise KeyError(f"输入版本 {version_id} 不存在")
    return json.loads(row["manifest_json"])


def list_versions(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT id, created_at, reason, detail, manifest_hash, superseded_by"
        " FROM input_versions ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]
