"""虚构数据生成器（确定性，随机种子固定）。

虚构场景：电商「星澜商城」的指标 checkout_conversion（下单转化率 = 支付订单 / 会话），
按渠道细分：app / web / mini（小程序）。时间：2026-09-01 .. 2026-10-30（60 天）。

数据中埋藏了三类相互独立的结构（生成器知道真相，但分析代码只看证据）：
  1. 上游数据缺口：2026-10-17..10-19 web 渠道批次只到了 45%（其余在 pending_redeliveries）。
  2. 口径变更：2026-10-17 起订单口径从 v1（含24h内取消）切换为 v2（剔除24h内取消）。
  3. 业务结构变化：2026-10-10 起小程序会话占比持续爬升（1500 -> 4000），其转化率偏低。
另有一段历史缺口（09-20 mini 到 80%，次日已重传补齐），用于演示补齐机制的历史形态。
"""
import random
from datetime import date, timedelta

from .db import now_iso

METRIC = "checkout_conversion"
START = date(2026, 9, 1)
N_DAYS = 60
SOURCES = ("app", "web", "mini")

GAP_DAYS = {date(2026, 10, 17) + timedelta(days=i) for i in range(3)}  # 10-17..10-19
GAP_SOURCE = "web"
GAP_RECEIVED_RATIO = 0.45

CALIBER_DAY = date(2026, 10, 17)  # v2 生效日

RAMP_START = date(2026, 10, 10)
RAMP_END = date(2026, 10, 30)
MINI_SESS_LO, MINI_SESS_HI = 1500, 4000

HIST_GAP_DAY = date(2026, 9, 20)   # 历史缺口（已补齐）
HIST_SOURCE = "mini"
HIST_RATIO = 0.80

CONV = {"app": 0.080, "web": 0.140, "mini": 0.060}
SESS_BASE = {"app": 5000, "web": 3000, "mini": 1500}
SESS_NOISE = {"app": 90, "web": 70, "mini": 50}


def day_str(d: date) -> str:
    return d.isoformat()


def mini_sessions(d: date, rng: random.Random) -> int:
    if d < RAMP_START:
        base = MINI_SESS_LO
    elif d >= RAMP_END:
        base = MINI_SESS_HI
    else:
        span = (RAMP_END - RAMP_START).days
        base = MINI_SESS_LO + (MINI_SESS_HI - MINI_SESS_LO) * (d - RAMP_START).days / span
    return int(round(base + rng.gauss(0, SESS_NOISE["mini"])))


def gen_truth() -> dict:
    """生成每天的「真实」完整数据：truth[day][source] = {sessions, orders_paid, cancelled24}"""
    rng = random.Random(20261017)
    truth = {}
    for i in range(N_DAYS):
        d = START + timedelta(days=i)
        row = {}
        for src in SOURCES:
            if src == "mini":
                sess = mini_sessions(d, rng)
            else:
                sess = int(round(SESS_BASE[src] + rng.gauss(0, SESS_NOISE[src])))
            conv = CONV[src] + rng.gauss(0, 0.0015)
            orders = int(round(sess * conv))
            cancelled = int(round(orders * (0.06 + rng.gauss(0, 0.003))))
            row[src] = {"sessions": sess, "orders_paid": orders, "orders_cancelled24": cancelled}
        truth[day_str(d)] = row
    return truth


def seed(conn) -> None:
    """向空库写入全部虚构数据与初始输入版本。幂等：已有数据则直接返回。"""
    if conn.execute("SELECT COUNT(*) c FROM input_versions").fetchone()["c"]:
        return
    truth = gen_truth()
    ts = now_iso()

    # --- 口径定义（只增） ---
    conn.execute(
        "INSERT INTO definition_versions(metric, version, spec_json) VALUES(?,?,?)",
        (METRIC, "v1", '{"label": "支付订单（含24小时内取消）", "orders_basis": "paid"}'),
    )
    conn.execute(
        "INSERT INTO definition_versions(metric, version, spec_json) VALUES(?,?,?)",
        (METRIC, "v2", '{"label": "支付订单（剔除24小时内取消）", "orders_basis": "paid_minus_cancelled24"}'),
    )
    conn.execute(
        "INSERT INTO caliber_events(metric, action, day, version, ref_event_id, created_at)"
        " VALUES(?,?,?,?,NULL,?)",
        (METRIC, "apply", day_str(CALIBER_DAY), "v2", ts),
    )

    # --- 批次 + 接入日志 + 待补齐 ---
    for dstr, per_src in truth.items():
        d = date.fromisoformat(dstr)
        for src, vals in per_src.items():
            sess, paid, canc = vals["sessions"], vals["orders_paid"], vals["orders_cancelled24"]

            def add_batch(part_sess, part_paid, part_canc, kind, log_status, note=None):
                bid = f"{src}-{dstr}-{kind}"
                conn.execute(
                    "INSERT INTO batches(batch_id, source, day, sessions, orders_paid,"
                    " orders_cancelled24, arrived_at, kind) VALUES(?,?,?,?,?,?,?,?)",
                    (bid, src, dstr, part_sess, part_paid, part_canc, ts, kind),
                )
                conn.execute(
                    "INSERT INTO ingest_log(source, day, expected_sessions, received_sessions,"
                    " status, batch_id, note, logged_at) VALUES(?,?,?,?,?,?,?,?)",
                    (src, dstr, sess, part_sess, log_status, bid, note, ts),
                )

            if d in GAP_DAYS and src == GAP_SOURCE:
                # 当前缺口：只投递一部分，其余进入待补齐
                rs, rp, rc = (int(round(sess * GAP_RECEIVED_RATIO)),
                              int(round(paid * GAP_RECEIVED_RATIO)),
                              int(round(canc * GAP_RECEIVED_RATIO)))
                add_batch(rs, rp, rc, "original", "partial",
                          note="上游批次截断，部分记录未送达")
                conn.execute(
                    "INSERT INTO pending_redeliveries(source, day, sessions, orders_paid,"
                    " orders_cancelled24) VALUES(?,?,?,?,?)",
                    (src, dstr, sess - rs, paid - rp, canc - rc),
                )
            elif d == HIST_GAP_DAY and src == HIST_SOURCE:
                # 历史缺口：先到 80%，随后重传补齐（演示补齐机制的完整生命周期）
                rs, rp, rc = (int(round(sess * HIST_RATIO)),
                              int(round(paid * HIST_RATIO)),
                              int(round(canc * HIST_RATIO)))
                add_batch(rs, rp, rc, "original", "partial",
                          note="上游批次截断，部分记录未送达")
                add_batch(sess - rs, paid - rp, canc - rc, "redelivery", "backfilled",
                          note="上游重传补齐")
            else:
                add_batch(sess, paid, canc, "original", "ok")

    conn.commit()

    # --- 初始输入版本 + 异常检测 + 初始分析任务 ---
    from .versions import create_input_version
    from .detect import detect_anomalies
    from .services import AttributionService

    vid = create_input_version(conn, "initial", "初始数据装载")
    svc = AttributionService(conn)
    series = svc.daily_series(vid, basis="active")
    anomalies = detect_anomalies(series)
    for a in anomalies:
        conn.execute(
            "INSERT INTO anomalies(metric, day, pre_window, post_window, delta,"
            " detected_on_version) VALUES(?,?,?,?,?,?)",
            (METRIC, a["day"], a["pre_window"], a["post_window"], a["delta"], vid),
        )
    conn.commit()

    # 初始报告：同步跑一个完整分析任务，保证首页即有候选解释
    row = conn.execute("SELECT id FROM anomalies ORDER BY id LIMIT 1").fetchone()
    if row:
        from .jobs import create_rerun_job, run_job_to_completion
        job_id = create_rerun_job(conn, row["id"], vid,
                                  ["upstream_gap", "caliber_change", "mix_shift"])
        run_job_to_completion(conn, job_id)
