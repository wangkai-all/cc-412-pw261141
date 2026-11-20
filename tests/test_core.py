"""核心行为测试：分解恒等式、双跑、缓存失效与沿用、任务断点续跑、修订流转。
运行：python3 -m unittest discover -s tests -v
"""
import json
import os
import tempfile
import threading
import unittest

from server.db import connect, init_db
from server import jobs
from server.jobs import _run, create_rerun_job, recover_after_restart
from server.seed import seed
from server.services import AttributionService, DependencyTracker, IngestLogService
from server.versions import current_version_id


class Base(unittest.TestCase):
    def setUp(self):
        jobs.STEP_GAP = 0  # 测试不等停顿
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "t.db")
        self.conn = connect(self.db_path)
        init_db(self.conn)
        seed(self.conn)
        self.svc = AttributionService(self.conn)
        self.vid = current_version_id(self.conn)
        self.anomaly = dict(self.conn.execute("SELECT * FROM anomalies LIMIT 1").fetchone())

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def report(self):
        return self.svc.latest_report(self.anomaly["id"])

    def candidate(self, kind):
        rep = self.report()
        return next(c for c in rep["candidates"] if c["kind"] == kind)


class TestDecomposition(Base):
    def test_bennet_identity_closes(self):
        """三项分解恒等闭合：水平+结构+交互 == 实测落差（两个视图都成立）。"""
        mix = self.candidate("mix_shift")
        for view in ("raw_view", "adjusted_view"):
            v = mix["evidence"][view]
            # 未四舍五入的恒等式残差必须≈0（机器精度）
            self.assertAlmostEqual(v["identity_residual"], 0.0, places=9)
            # 展示值各自保留 3 位小数，合计允许 0.005 以内的舍入累计
            self.assertAlmostEqual(
                v["within_pp"] + v["mix_pp"] + v["interaction_pp"], v["delta_pp"],
                delta=0.005)

    def test_adjusted_view_isolates_structure(self):
        """剔除缺口与口径后，水平项应接近 0（虚构数据各来源转化率本身稳定）。"""
        mix = self.candidate("mix_shift")
        adj = mix["evidence"]["adjusted_view"]
        self.assertLess(abs(adj["within_pp"]), 0.15)
        self.assertLess(adj["mix_pp"], -0.1)  # 结构项为负：小程序占比上升拉低整体


class TestCaliberDualRun(Base):
    def test_dual_run_direction_and_magnitude(self):
        cal = self.candidate("caliber_change")
        ev = cal["evidence"]
        self.assertLess(ev["dual_run_post_mean_delta_pp"], 0)  # v2 剔除取消单 → 更低
        self.assertGreater(ev["dual_run_post_mean_delta_pp"], -1.0)
        # 前窗口两口径差值也应稳定（取消占比均匀），作为对照
        self.assertAlmostEqual(ev["dual_run_pre_mean_delta_pp"],
                               ev["dual_run_post_mean_delta_pp"], delta=0.2)
        for d in ev["dual_run_post"]:
            self.assertLessEqual(d["rate_v2"], d["rate_v1"])


class TestGapLens(Base):
    def test_gap_supported_with_positive_impact(self):
        gap = self.candidate("upstream_gap")
        self.assertEqual(gap["status"], "supported")
        self.assertGreater(gap["estimated_impact_pp"], 0.1)
        days = {g["day"] for g in gap["evidence"]["gap_entries"]}
        self.assertEqual(days, {"2026-10-17", "2026-10-18", "2026-10-19"})


class TestCacheAndVersions(Base):
    def test_revision_forces_recompute_no_stale_reuse(self):
        """补齐缺口 → 新输入版本 → 受影响镜头全部重算，旧缓存仅供审计不再命中。"""
        # 先制造一次缓存命中（同版本重跑）
        _, prov0 = self.svc.run_lens(self.vid, self.anomaly, "upstream_gap")
        self.assertEqual(prov0, "cache_hit")

        IngestLogService(self.conn).backfill("web", "2026-10-17")
        rev = self.svc.apply_revision("backfill", "测试补齐")
        self.assertIn("upstream_gap", rev["affected"])
        new_vid = rev["new_version"]
        self.assertNotEqual(new_vid, self.vid)

        _, prov = self.svc.run_lens(new_vid, self.anomaly, "upstream_gap")
        self.assertEqual(prov, "recomputed")  # 旧证据不可复用
        # 旧版本缓存仍在（审计），但查询钉在新版本，永远不会命中它
        keys = [r["cache_key"] for r in self.conn.execute(
            "SELECT cache_key FROM comparison_cache WHERE input_version_id=?",
            (self.vid,)).fetchall()]
        self.assertTrue(keys)
        row = self.conn.execute(
            "SELECT COUNT(*) c FROM comparison_cache WHERE input_version_id=?",
            (new_vid,)).fetchone()
        self.assertEqual(row["c"], 1)

    def test_carry_forward_only_for_unaffected(self):
        """撤回口径：缺口镜头依赖未变可沿用；口径/结构镜头必须重算。"""
        tracker = DependencyTracker(self.conn)
        ev = [e for e in tracker.events() if e["action"] == "apply"][0]
        tracker.retract(ev["id"])
        rev = self.svc.apply_revision("retract_caliber", "测试撤回")
        self.assertEqual(sorted(rev["affected"]), ["caliber_change", "mix_shift"])

        carried = self.svc.carry_forward(rev["old_version"], rev["new_version"],
                                         self.anomaly, ["upstream_gap"])
        self.assertEqual(carried, ["upstream_gap"])
        _, prov_gap = self.svc.run_lens(rev["new_version"], self.anomaly, "upstream_gap")
        self.assertEqual(prov_gap, "carried_forward")
        _, prov_cal = self.svc.run_lens(rev["new_version"], self.anomaly, "caliber_change")
        self.assertEqual(prov_cal, "recomputed")

    def test_backfill_removes_gap(self):
        ingest = IngestLogService(self.conn)
        for g in ingest.pending_gaps():
            ingest.backfill(g["source"], g["day"])
        rev = self.svc.apply_revision("backfill", "全部补齐")
        payload, _ = self.svc.run_lens(rev["new_version"], self.anomaly, "upstream_gap")
        self.assertEqual(payload["evidence"]["gap_entries"], [])
        self.assertLess(abs(payload["estimated_impact_pp"]), 0.1)


class TestJobInterruptResume(Base):
    def test_interrupt_then_resume_skips_done_steps(self):
        vid = self.vid
        job_id = create_rerun_job(self.conn, self.anomaly["id"], vid,
                                  ["upstream_gap", "caliber_change", "mix_shift"])
        flag = threading.Event()
        # 第一步完成后置中断标记
        original = jobs._exec_step
        calls = {"n": 0}

        def wrapped(conn, job, step):
            detail = original(conn, job, step)
            calls["n"] += 1
            if calls["n"] == 1:
                flag.set()
            return detail

        jobs._exec_step = wrapped
        try:
            _run(self.conn, job_id, flag)
        finally:
            jobs._exec_step = original

        job = dict(self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
        self.assertEqual(job["status"], "interrupted")
        self.assertEqual(job["current_step"], 1)
        steps = json.loads(job["steps_json"])
        self.assertEqual(steps[0]["status"], "done")
        finished_at_step0 = steps[0]["finished_at"]
        self.assertIsNotNone(finished_at_step0)

        # 续跑：从第 2 步开始，第 1 步不重做
        _run(self.conn, job_id, threading.Event())
        job = dict(self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
        self.assertEqual(job["status"], "done")
        steps = json.loads(job["steps_json"])
        self.assertEqual(steps[0]["finished_at"], finished_at_step0)
        self.assertTrue(all(s["status"] == "done" for s in steps))
        self.assertIsNotNone(self.report())

    def test_recover_marks_running_interrupted(self):
        job_id = create_rerun_job(self.conn, self.anomaly["id"], self.vid, ["upstream_gap"])
        self.conn.execute("UPDATE jobs SET status='running' WHERE id=?", (job_id,))
        self.conn.commit()
        recover_after_restart(self.db_path)
        row = self.conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        self.assertEqual(row["status"], "interrupted")


class TestRetractFlow(Base):
    def test_retract_restores_v1_series(self):
        tracker = DependencyTracker(self.conn)
        ev = [e for e in tracker.events() if e["action"] == "apply"][0]
        tracker.retract(ev["id"])
        rev = self.svc.apply_revision("retract_caliber", "测试")
        series = self.svc.daily_series(rev["new_version"], basis="active")
        self.assertTrue(all(s["basis"] == "v1" for s in series))
        # 撤回后口径镜头应标注 retracted
        payload, _ = self.svc.run_lens(rev["new_version"], self.anomaly, "caliber_change")
        self.assertEqual(payload["status"], "retracted")


if __name__ == "__main__":
    unittest.main()
