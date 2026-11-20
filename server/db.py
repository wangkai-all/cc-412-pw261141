"""SQLite 存储层。所有证据表（批次、接入日志、口径事件）均为只增不改（append-only），
修订（补齐缺口、撤回口径）只会追加新行并产生新的输入版本，旧版本因此永远可重算。"""
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("ATTRIB_DB", os.path.join(BASE_DIR, "data", "app.db"))

SCHEMA = """
-- 上游投递的原始批次（只增）。redelivery 为上游重传批次。
CREATE TABLE IF NOT EXISTS batches(
  batch_id TEXT PRIMARY KEY,
  source TEXT NOT NULL,              -- 渠道/来源: app | web | mini
  day TEXT NOT NULL,                 -- YYYY-MM-DD
  sessions INTEGER NOT NULL,
  orders_paid INTEGER NOT NULL,          -- 支付成功订单（含24h内取消）
  orders_cancelled24 INTEGER NOT NULL,   -- 其中24h内取消
  arrived_at TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'original'  -- original | redelivery
);

-- 接入日志服务：每个来源每天应到/实到的会话量（只增）。
CREATE TABLE IF NOT EXISTS ingest_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  day TEXT NOT NULL,
  expected_sessions INTEGER NOT NULL,
  received_sessions INTEGER NOT NULL,
  status TEXT NOT NULL,              -- ok | partial | missing | backfilled
  batch_id TEXT,
  note TEXT,
  logged_at TEXT NOT NULL
);

-- 待补齐的上游重传（模拟上游可重新投递的缺口部分）。
CREATE TABLE IF NOT EXISTS pending_redeliveries(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  day TEXT NOT NULL,
  sessions INTEGER NOT NULL,
  orders_paid INTEGER NOT NULL,
  orders_cancelled24 INTEGER NOT NULL
);

-- 指标口径定义版本（只增，永不修改）。
CREATE TABLE IF NOT EXISTS definition_versions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  metric TEXT NOT NULL,
  version TEXT NOT NULL,
  spec_json TEXT NOT NULL            -- {"label": ..., "orders_basis": "paid" | "paid_minus_cancelled24"}
);

-- 口径事件（只增）：apply=某口径自某天起生效；retract=撤回某次 apply。
CREATE TABLE IF NOT EXISTS caliber_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  metric TEXT NOT NULL,
  action TEXT NOT NULL,              -- apply | retract
  day TEXT NOT NULL,
  version TEXT NOT NULL,
  ref_event_id INTEGER,              -- retract 指向被撤回的 apply 事件
  created_at TEXT NOT NULL
);

-- 输入版本：一份清单（批次集合 + 日志水位 + 口径事件水位 + 解析后的口径时间线）。
-- 任何分析都钉在某一个输入版本上重算；修订只产生新版本，旧版本完整可重放。
CREATE TABLE IF NOT EXISTS input_versions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  reason TEXT NOT NULL,              -- initial | backfill | retract_caliber
  detail TEXT,
  manifest_json TEXT NOT NULL,
  manifest_hash TEXT NOT NULL,
  superseded_by INTEGER
);

CREATE TABLE IF NOT EXISTS anomalies(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  metric TEXT NOT NULL,
  day TEXT NOT NULL,
  pre_window TEXT NOT NULL,          -- "YYYY-MM-DD..YYYY-MM-DD"
  post_window TEXT NOT NULL,
  delta REAL NOT NULL,
  detected_on_version INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'open'
);

-- 对照缓存：键 = (镜头, 输入版本, 代码版本, 参数)。修订产生新输入版本后，
-- 旧键永远不会被命中（查询总是钉在当前版本），从机制上杜绝复用过期证据；
-- 旧条目保留仅供审计，界面标注"已被取代"。
CREATE TABLE IF NOT EXISTS comparison_cache(
  cache_key TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  input_version_id INTEGER NOT NULL,
  -- 结果实际计算时所用的输入版本；与 input_version_id 不同表示「依赖未变、沿用旧版结果」
  source_version_id INTEGER NOT NULL,
  code_version TEXT NOT NULL,
  params_hash TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  hits INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

-- 可中断任务：步骤级检查点，中断后从下一个未完成步骤继续。
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,                -- rerun_anomaly
  anomaly_id INTEGER,
  input_version_id INTEGER NOT NULL,
  affected_lenses TEXT NOT NULL,     -- JSON: 本次重跑覆盖的镜头
  status TEXT NOT NULL DEFAULT 'pending',  -- pending|running|interrupted|done|failed
  steps_json TEXT NOT NULL,          -- [{name,label,status,detail,finished_at}]
  current_step INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reports(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  anomaly_id INTEGER NOT NULL,
  input_version_id INTEGER NOT NULL,
  job_id INTEGER,
  created_at TEXT NOT NULL,
  payload_json TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str | None = None) -> sqlite3.Connection:
    p = path or DB_PATH
    os.makedirs(os.path.dirname(p), exist_ok=True)
    conn = sqlite3.connect(p, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def sha256_json(obj) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def row_to_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}
