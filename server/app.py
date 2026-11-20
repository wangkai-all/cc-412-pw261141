"""HTTP 服务：网页 + JSON API。仅标准库。启动：python3 -m server.app [port]"""
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .db import DB_PATH, connect, init_db
from .jobs import (create_rerun_job, interrupt_job, recover_after_restart,
                   resume_job, start_job_background)
from .seed import METRIC, seed
from .services import (AttributionService, DependencyTracker, IngestLogService)
from .versions import current_version_id, list_versions

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


def overview(conn) -> dict:
    svc = AttributionService(conn)
    vid = current_version_id(conn)
    anomalies = [dict(r) for r in conn.execute(
        "SELECT * FROM anomalies ORDER BY id").fetchall()]
    jobs = []
    for r in conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 10").fetchall():
        d = dict(r)
        d["steps"] = json.loads(d["steps_json"])
        d.pop("steps_json")
        jobs.append(d)
    cache_rows = conn.execute(
        "SELECT kind, input_version_id, source_version_id, hits, size_bytes, created_at"
        " FROM comparison_cache ORDER BY created_at").fetchall()
    reports = [dict(r) for r in conn.execute(
        "SELECT id, anomaly_id, input_version_id, job_id, created_at FROM reports"
        " ORDER BY id DESC LIMIT 5").fetchall()]
    return {
        "metric": METRIC,
        "current_version_id": vid,
        "series": svc.daily_series(vid, basis="active"),
        "anomalies": anomalies,
        "pending_gaps": IngestLogService(conn).pending_gaps(),
        "caliber_events": DependencyTracker(conn).events(),
        "definitions": DependencyTracker(conn).definitions(),
        "dependency_graph": DependencyTracker.dependency_graph(),
        "versions": list_versions(conn),
        "jobs": jobs,
        "cache": [dict(r) for r in cache_rows],
        "reports": reports,
    }


def do_backfill(conn, db_path: str, body: dict) -> dict:
    ingest = IngestLogService(conn)
    svc = AttributionService(conn)
    targets = ingest.pending_gaps() if body.get("all") else [
        {"source": body["source"], "day": body["day"]}]
    applied = []
    for t in targets:
        r = ingest.backfill(t["source"], t["day"])
        if r:
            applied.append(r)
    if not applied:
        return {"error": "没有可补齐的缺口"}, 409
    detail = "补齐缺口：" + "、".join(f"{a['source']}@{a['day']}" for a in applied)
    rev = svc.apply_revision("backfill", detail)
    return _enqueue_rerun(conn, db_path, rev, detail)


def do_retract(conn, db_path: str, body: dict) -> dict:
    tracker = DependencyTracker(conn)
    svc = AttributionService(conn)
    ev = tracker.retract(int(body["event_id"]))
    if not ev:
        return {"error": "口径事件不存在或已被撤回"}, 409
    detail = f"撤回口径变更：{ev['version']}（原生效日 {ev['day']}）"
    rev = svc.apply_revision("retract_caliber", detail)
    return _enqueue_rerun(conn, db_path, rev, detail)


def _enqueue_rerun(conn, db_path: str, rev: dict, detail: str) -> dict:
    """修订后：依赖未变的镜头沿用（依赖追踪器证明），受影响的入队重算。"""
    svc = AttributionService(conn)
    anomaly = conn.execute(
        "SELECT * FROM anomalies ORDER BY id LIMIT 1").fetchone()
    carried = []
    if anomaly:
        unaffected = [k for k in ("upstream_gap", "caliber_change", "mix_shift")
                      if k not in rev["affected"]]
        carried = svc.carry_forward(rev["old_version"], rev["new_version"],
                                    dict(anomaly), unaffected)
    job_id = create_rerun_job(conn, anomaly["id"], rev["new_version"], rev["affected"])
    start_job_background(db_path, job_id)
    return {"revision": detail, "new_version": rev["new_version"],
            "affected_lenses": rev["affected"], "carried_forward": carried,
            "job_id": job_id}, 200


def do_rerun(conn, db_path: str, anomaly_id: int) -> dict:
    vid = current_version_id(conn)
    affected = ["upstream_gap", "caliber_change", "mix_shift"]
    job_id = create_rerun_job(conn, anomaly_id, vid, affected)
    start_job_background(db_path, job_id)
    return {"job_id": job_id, "input_version_id": vid, "affected_lenses": affected}, 200


class Handler(BaseHTTPRequestHandler):
    db_path = DB_PATH

    # ---------- 工具 ----------
    def _json(self, obj, status=200):
        blob = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode())

    def log_message(self, *a):  # 静默访问日志
        pass

    # ---------- GET ----------
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            return self._static("index.html", "text/html; charset=utf-8")
        if path.startswith("/static/"):
            name = path[len("/static/"):]
            ctype = {"js": "text/javascript", "css": "text/css"}.get(
                name.rsplit(".", 1)[-1], "text/plain")
            return self._static(name, ctype)
        conn = connect(self.db_path)
        try:
            if path == "/api/overview":
                return self._json(overview(conn))
            m = re.fullmatch(r"/api/anomalies/(\d+)/report", path)
            if m:
                rep = AttributionService(conn).latest_report(int(m.group(1)))
                return self._json(rep or {"error": "尚无报告"}, 200 if rep else 404)
            m = re.fullmatch(r"/api/jobs/(\d+)", path)
            if m:
                row = conn.execute("SELECT * FROM jobs WHERE id=?",
                                   (int(m.group(1)),)).fetchone()
                if not row:
                    return self._json({"error": "任务不存在"}, 404)
                d = dict(row)
                d["steps"] = json.loads(d["steps_json"])
                d.pop("steps_json")
                return self._json(d)
            return self._json({"error": "not found"}, 404)
        finally:
            conn.close()

    def _static(self, name: str, ctype: str):
        p = os.path.join(WEB_DIR, name)
        if not os.path.isfile(p):
            return self._json({"error": "not found"}, 404)
        with open(p, "rb") as f:
            blob = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    # ---------- POST ----------
    def do_POST(self):
        path = self.path.split("?")[0]
        conn = connect(self.db_path)
        try:
            body = self._body()
            if path == "/api/gaps/backfill":
                out, status = do_backfill(conn, self.db_path, body)
                return self._json(out, status)
            if path == "/api/caliber/retract":
                out, status = do_retract(conn, self.db_path, body)
                return self._json(out, status)
            m = re.fullmatch(r"/api/anomalies/(\d+)/rerun", path)
            if m:
                out, status = do_rerun(conn, self.db_path, int(m.group(1)))
                return self._json(out, status)
            m = re.fullmatch(r"/api/jobs/(\d+)/(resume|interrupt)", path)
            if m:
                job_id, action = int(m.group(1)), m.group(2)
                if action == "resume":
                    ok = resume_job(self.db_path, job_id)
                    return self._json({"resumed": ok}, 200 if ok else 409)
                ok = interrupt_job(job_id)
                return self._json({"interrupted": ok}, 200 if ok else 409)
            return self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            return self._json({"error": str(e)}, 500)
        finally:
            conn.close()


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8412
    conn = connect(DB_PATH)
    init_db(conn)
    seed(conn)
    conn.close()
    recover_after_restart(DB_PATH)
    Handler.db_path = DB_PATH
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"归因工作台: http://127.0.0.1:{port}  (数据库 {DB_PATH})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
