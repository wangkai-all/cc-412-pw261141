"""可中断任务：步骤级检查点落库，中断/崩溃后从下一个未完成步骤继续。

- 每步执行前检查中断标记；执行后立即提交检查点（current_step 前进）。
- 服务重启时把遗留 running 任务标记为 interrupted，可由界面「继续」恢复。
- 步骤故意留有短暂停顿，让中断与进度在界面上可观察。
"""
import json
import threading
import time

from .db import connect, now_iso
from .lenses import LENS_TITLES
from .services import AttributionService

STEP_GAP = 0.6  # 每步之间的停顿（秒），便于观察进度与中断

_interrupt_flags: dict[int, threading.Event] = {}
_threads: dict[int, threading.Thread] = {}
_lock = threading.Lock()


def _steps_for(affected: list[str]) -> list[dict]:
    steps = [{"name": "pin_input_version", "label": "钉住输入版本与依赖快照"}]
    for kind in affected:
        steps.append({"name": f"run_{kind}", "label": f"重算对照：{LENS_TITLES[kind]}",
                      "lens": kind})
    steps.append({"name": "compose_report", "label": "汇总归因报告（候选解释）"})
    for s in steps:
        s.update({"status": "pending", "detail": None, "finished_at": None})
    return steps


def create_rerun_job(conn, anomaly_id: int, version_id: int, affected: list[str]) -> int:
    ts = now_iso()
    conn.execute(
        "INSERT INTO jobs(kind, anomaly_id, input_version_id, affected_lenses, status,"
        " steps_json, current_step, created_at, updated_at) VALUES(?,?,?,?,?,?,0,?,?)",
        ("rerun_anomaly", anomaly_id, version_id, json.dumps(affected),
         "pending", json.dumps(_steps_for(affected), ensure_ascii=False), ts, ts))
    conn.commit()
    return conn.execute("SELECT last_insert_rowid() i").fetchone()["i"]


def _load(conn, job_id: int) -> dict:
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise KeyError(f"任务 {job_id} 不存在")
    d = dict(row)
    d["steps"] = json.loads(d["steps_json"])
    return d


def _save_steps(conn, job_id: int, steps: list[dict], current_step: int,
                status: str | None = None, error: str | None = None):
    if status:
        conn.execute(
            "UPDATE jobs SET steps_json=?, current_step=?, status=?, error=?, updated_at=?"
            " WHERE id=?",
            (json.dumps(steps, ensure_ascii=False), current_step, status, error,
             now_iso(), job_id))
    else:
        conn.execute(
            "UPDATE jobs SET steps_json=?, current_step=?, updated_at=? WHERE id=?",
            (json.dumps(steps, ensure_ascii=False), current_step, now_iso(), job_id))
    conn.commit()


def _exec_step(conn, job: dict, step: dict) -> dict:
    """执行单个步骤，返回要写入检查点的 detail。"""
    svc = AttributionService(conn)
    anomaly = dict(conn.execute(
        "SELECT * FROM anomalies WHERE id=?", (job["anomaly_id"],)).fetchone())
    vid = job["input_version_id"]
    if step["name"] == "pin_input_version":
        from .versions import get_manifest
        m = get_manifest(conn, vid)
        return {"input_version_id": vid,
                "batches": len(m["batch_ids"]),
                "ingest_log_max_id": m["ingest_log_max_id"],
                "caliber_event_max_id": m["caliber_event_max_id"],
                "definition_spans": m["definition_spans"]}
    if step["name"].startswith("run_"):
        payload, prov = svc.run_lens(vid, anomaly, step["lens"])
        return {"lens": step["lens"], "provenance": prov,
                "status": payload["status"],
                "estimated_impact_pp": payload["estimated_impact_pp"]}
    if step["name"] == "compose_report":
        provenance = {s["detail"]["lens"]: s["detail"]["provenance"]
                      for s in job["steps"] if s.get("detail") and s["detail"].get("lens")}
        report_id = svc.compose_report(job["anomaly_id"], vid, job["id"], provenance)
        return {"report_id": report_id}
    raise ValueError(f"未知步骤 {step['name']}")


def _run(conn, job_id: int, flag: threading.Event):
    try:
        conn.execute("UPDATE jobs SET status='running', updated_at=? WHERE id=?",
                     (now_iso(), job_id))
        conn.commit()
        while True:
            job = _load(conn, job_id)
            idx = job["current_step"]
            if idx >= len(job["steps"]):
                _save_steps(conn, job_id, job["steps"], idx, status="done")
                return
            if flag.is_set():
                _save_steps(conn, job_id, job["steps"], idx, status="interrupted")
                return
            step = job["steps"][idx]
            step["status"] = "running"
            _save_steps(conn, job_id, job["steps"], idx)
            time.sleep(STEP_GAP)  # 可观察的停顿；中断在步骤边界生效
            if flag.is_set():
                step["status"] = "pending"
                _save_steps(conn, job_id, job["steps"], idx, status="interrupted")
                return
            detail = _exec_step(conn, job, step)
            step["status"] = "done"
            step["detail"] = detail
            step["finished_at"] = now_iso()
            _save_steps(conn, job_id, job["steps"], idx + 1)
    except Exception as e:  # noqa: BLE001 - 任务失败要落库而不是静默
        try:
            job = _load(conn, job_id)
            _save_steps(conn, job_id, job["steps"], job["current_step"],
                        status="failed", error=str(e))
        except Exception:
            pass


def run_job_to_completion(conn, job_id: int):
    """同步执行（种子初始化用）。"""
    _run(conn, job_id, threading.Event())


def start_job_background(db_path: str, job_id: int):
    """后台线程执行（网页触发用）。线程内使用独立连接。"""
    flag = threading.Event()
    with _lock:
        _interrupt_flags[job_id] = flag

    def target():
        conn = connect(db_path)
        try:
            _run(conn, job_id, flag)
        finally:
            conn.close()
            with _lock:
                _interrupt_flags.pop(job_id, None)
                _threads.pop(job_id, None)

    t = threading.Thread(target=target, daemon=True, name=f"job-{job_id}")
    with _lock:
        _threads[job_id] = t
    t.start()


def interrupt_job(job_id: int) -> bool:
    with _lock:
        flag = _interrupt_flags.get(job_id)
    if flag:
        flag.set()
        return True
    return False


def resume_job(db_path: str, job_id: int) -> bool:
    conn = connect(db_path)
    try:
        job = _load(conn, job_id)
        if job["status"] not in ("interrupted", "pending", "failed"):
            return False
        conn.execute("UPDATE jobs SET status='pending', error=NULL, updated_at=? WHERE id=?",
                     (now_iso(), job_id))
        conn.commit()
    finally:
        conn.close()
    start_job_background(db_path, job_id)
    return True


def recover_after_restart(db_path: str):
    """服务重启：上次进程死亡时遗留的 running 任务标记为 interrupted，等待继续。"""
    conn = connect(db_path)
    try:
        conn.execute(
            "UPDATE jobs SET status='interrupted', updated_at=? WHERE status IN ('running','pending')",
            (now_iso(),))
        conn.commit()
    finally:
        conn.close()
