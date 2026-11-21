import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import Config

SCHEMA = """
CREATE TABLE IF NOT EXISTS lineage_nodes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  node_code TEXT NOT NULL UNIQUE,
  table_name TEXT NOT NULL,
  layer TEXT NOT NULL CHECK (layer IN ('ODS','DWD','DWS','ADS')),
  owner_team TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK (status IN ('登记','在用','停用')),
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS lineage_edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  edge_code TEXT NOT NULL UNIQUE,
  upstream_id INTEGER NOT NULL REFERENCES lineage_nodes(id),
  downstream_id INTEGER NOT NULL REFERENCES lineage_nodes(id),
  transform_kind TEXT NOT NULL DEFAULT '直抽',
  status TEXT NOT NULL CHECK (status IN ('草稿','已发布','已作废')),
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(upstream_id, downstream_id)
);

CREATE TABLE IF NOT EXISTS cut_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  upstream_id INTEGER NOT NULL REFERENCES lineage_nodes(id),
  upstream_code TEXT NOT NULL,
  fate_kind TEXT NOT NULL CHECK (fate_kind IN ('旁路凭证','归档物化包')),
  fingerprint TEXT NOT NULL,
  downstream_ids TEXT NOT NULL DEFAULT '[]',
  downstream_codes TEXT NOT NULL DEFAULT '[]',
  edge_codes TEXT NOT NULL DEFAULT '[]',
  sink_ref TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK (status IN ('生效','封存','已撤销')),
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 串行闸：同一上游只允许一份有效命运行（并发抢点切断时由数据库兜底）
CREATE UNIQUE INDEX IF NOT EXISTS ux_cut_events_active_upstream
  ON cut_events(upstream_id) WHERE status != '已撤销';

-- 落点一：旁路凭证表（切断当时的下游集合，影响分析不得再读它）
CREATE TABLE IF NOT EXISTS cut_bypass_vouchers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cut_id INTEGER NOT NULL REFERENCES cut_events(id),
  upstream_id INTEGER NOT NULL REFERENCES lineage_nodes(id),
  downstream_id INTEGER NOT NULL REFERENCES lineage_nodes(id),
  downstream_code TEXT NOT NULL,
  voucher_no TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(cut_id, downstream_id)
);

-- 旁路凭证收的是切断当时的快照，只可读，不可改写/删除
CREATE TRIGGER IF NOT EXISTS cut_bypass_vouchers_readonly_update
BEFORE UPDATE ON cut_bypass_vouchers
BEGIN
  SELECT RAISE(ABORT, '旁路凭证为切断快照，不可改写');
END;

CREATE TRIGGER IF NOT EXISTS cut_bypass_vouchers_readonly_delete
BEFORE DELETE ON cut_bypass_vouchers
BEGIN
  SELECT RAISE(ABORT, '旁路凭证为切断快照，不可删除');
END;

-- 落点二：只读归档层的物化包（封存切断指纹，不可改写/删除）
CREATE TABLE IF NOT EXISTS cut_archive_packages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cut_id INTEGER NOT NULL REFERENCES cut_events(id),
  package_no TEXT NOT NULL UNIQUE,
  fingerprint TEXT NOT NULL,
  payload TEXT NOT NULL,
  sealed_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(cut_id)
);

CREATE TRIGGER IF NOT EXISTS cut_archive_packages_readonly_update
BEFORE UPDATE ON cut_archive_packages
BEGIN
  SELECT RAISE(ABORT, '归档物化包只读，不可改写');
END;

CREATE TRIGGER IF NOT EXISTS cut_archive_packages_readonly_delete
BEFORE DELETE ON cut_archive_packages
BEGIN
  SELECT RAISE(ABORT, '归档物化包只读，不可删除');
END;

CREATE TABLE IF NOT EXISTS impact_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_code TEXT NOT NULL UNIQUE,
  root_node_id INTEGER NOT NULL REFERENCES lineage_nodes(id),
  status TEXT NOT NULL CHECK (status IN ('待执行','执行中','成功','失败')),
  affected_count INTEGER NOT NULL DEFAULT 0,
  excluded_count INTEGER NOT NULL DEFAULT 0,
  message TEXT NOT NULL DEFAULT '',
  started_at TEXT,
  finished_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS impact_exclusions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES impact_jobs(id),
  node_id INTEGER NOT NULL REFERENCES lineage_nodes(id),
  cut_id INTEGER REFERENCES cut_events(id),
  reason TEXT NOT NULL DEFAULT '',
  UNIQUE(job_id, node_id)
);

CREATE TABLE IF NOT EXISTS impact_hits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES impact_jobs(id),
  node_id INTEGER NOT NULL REFERENCES lineage_nodes(id),
  hop INTEGER NOT NULL DEFAULT 0,
  UNIQUE(job_id, node_id)
);

CREATE TABLE IF NOT EXISTS notify_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES impact_jobs(id),
  node_id INTEGER NOT NULL REFERENCES lineage_nodes(id),
  channel TEXT NOT NULL DEFAULT '站内',
  status TEXT NOT NULL CHECK (status IN ('待发','已发','失败')),
  body TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS coverage_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  summary TEXT NOT NULL,
  node_count INTEGER NOT NULL,
  edge_count INTEGER NOT NULL,
  orphan_count INTEGER NOT NULL,
  uncovered_edges INTEGER NOT NULL,
  cut_count INTEGER NOT NULL DEFAULT 0,
  excluded_count INTEGER NOT NULL DEFAULT 0,
  check_total INTEGER NOT NULL DEFAULT 0,
  check_passed INTEGER NOT NULL DEFAULT 0,
  verdict TEXT NOT NULL DEFAULT '抽检未过',
  checks_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ingest_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mode TEXT NOT NULL DEFAULT '全量',
  status TEXT NOT NULL CHECK (status IN ('成功','拦截')),
  message TEXT NOT NULL DEFAULT '',
  node_count INTEGER NOT NULL DEFAULT 0,
  edge_count INTEGER NOT NULL DEFAULT 0,
  cut_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS lab_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  label TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


@contextmanager
def get_db(db_path: str | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_schema(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.executescript(SCHEMA)
