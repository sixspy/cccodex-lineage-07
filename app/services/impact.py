"""影响分析：从根节点沿已发布边向下展开。

旁路切断（旁路凭证落点）生效后，切断当时的下游集合不再被影响分析读取：
从任何根节点展开都不再穿过这些节点，被挡在切断后面的节点逐条落
impact_exclusions，影响页显示已剔除条数。
归档物化包落点不改现存路径，下游照旧参与分析。
"""
from __future__ import annotations

import json

from app.db import get_db
from app.services.errors import ServiceError


def list_jobs(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT j.*, n.node_code, n.table_name FROM impact_jobs j "
            "JOIN lineage_nodes n ON n.id=j.root_node_id ORDER BY j.id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def job_detail(job_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        job = conn.execute(
            "SELECT j.*, n.node_code, n.table_name FROM impact_jobs j "
            "JOIN lineage_nodes n ON n.id=j.root_node_id WHERE j.id=?",
            (job_id,),
        ).fetchone()
        if not job:
            raise ServiceError("作业不存在", 404)
        hits = conn.execute(
            "SELECT h.*, n.node_code, n.table_name, n.layer FROM impact_hits h "
            "JOIN lineage_nodes n ON n.id=h.node_id WHERE h.job_id=? ORDER BY h.hop, h.id",
            (job_id,),
        ).fetchall()
        excluded = conn.execute(
            "SELECT x.*, n.node_code, n.table_name, n.layer, c.upstream_code, c.fate_kind "
            "FROM impact_exclusions x "
            "JOIN lineage_nodes n ON n.id=x.node_id "
            "LEFT JOIN cut_events c ON c.id=x.cut_id "
            "WHERE x.job_id=? ORDER BY x.id",
            (job_id,),
        ).fetchall()
        return {
            "job": dict(job),
            "hits": [dict(x) for x in hits],
            "excluded": [dict(x) for x in excluded],
            "excluded_count": len(excluded),
        }


def bypass_removed(conn) -> dict[int, int]:
    """生效旁路切断剔除的节点 id → 所属切断 id。"""
    rows = conn.execute(
        "SELECT id, downstream_ids FROM cut_events "
        "WHERE status='生效' AND fate_kind='旁路凭证' ORDER BY id"
    ).fetchall()
    out: dict[int, int] = {}
    for r in rows:
        for nid in json.loads(r["downstream_ids"] or "[]"):
            out.setdefault(int(nid), int(r["id"]))
    return out


def _expand(conn, root_id: int, removed: dict[int, int], block: bool = True) -> dict[int, int]:
    """向下展开，返回 {node_id: hop}。block=True 时不穿过 removed 中的节点。"""
    seen = {root_id: 0}
    queue = [root_id]
    while queue:
        cur_id = queue.pop(0)
        if block and cur_id in removed:
            continue
        hop = seen[cur_id]
        kids = conn.execute(
            "SELECT downstream_id FROM lineage_edges "
            "WHERE upstream_id=? AND status='已发布' ORDER BY id",
            (cur_id,),
        ).fetchall()
        for kid in kids:
            kid_id = int(kid["downstream_id"])
            if kid_id not in seen:
                seen[kid_id] = hop + 1
                queue.append(kid_id)
    return seen


def start_impact(root_node_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        root = conn.execute(
            "SELECT * FROM lineage_nodes WHERE id=?", (root_node_id,)
        ).fetchone()
        if not root:
            raise ServiceError("根节点不存在", 404)
        if root["status"] == "停用":
            raise ServiceError("停用节点不可开影响分析", 409)

        removed = bypass_removed(conn)
        if int(root_node_id) in removed:
            raise ServiceError(
                "该节点已被上游旁路切断剔除，影响分析不再读取它按旧路径的依赖；"
                "如需复算请先从更上游的切断点开作业",
                409,
            )

        running = conn.execute(
            "SELECT id FROM impact_jobs WHERE root_node_id=? AND status IN ('待执行','执行中')",
            (root_node_id,),
        ).fetchone()
        if running:
            raise ServiceError("该根节点已有在途影响作业", 409)
        seq = conn.execute("SELECT COUNT(*) AS c FROM impact_jobs").fetchone()["c"] + 1
        job_code = f"IMP-{seq:04d}"
        cur = conn.execute(
            "INSERT INTO impact_jobs(job_code, root_node_id, status, message, started_at) "
            "VALUES (?,?,'执行中','展开中',datetime('now'))",
            (job_code, root_node_id),
        )
        job_id = cur.lastrowid

        full = _expand(conn, root_node_id, removed, block=False)
        seen = _expand(conn, root_node_id, removed)
        hits = {nid: hop for nid, hop in seen.items() if nid not in removed}
        # 被旁路切断收走的下游，只要原本在本次根节点的可达范围内，一律剔除
        excluded = {nid: cut_id for nid, cut_id in removed.items() if nid in full}

        for nid, hop in hits.items():
            conn.execute(
                "INSERT OR IGNORE INTO impact_hits(job_id, node_id, hop) VALUES (?,?,?)",
                (job_id, nid, hop),
            )
        for nid, cut_id in excluded.items():
            conn.execute(
                "INSERT OR IGNORE INTO impact_exclusions(job_id, node_id, cut_id, reason) "
                "VALUES (?,?,?,?)",
                (job_id, nid, cut_id, "上游切断：旁路落点剔除，不再按旧路径计入"),
            )

        affected = len(hits)
        excluded_count = len(excluded)
        message = f"影响到 {affected} 个节点"
        if excluded_count:
            message += f"；已剔除 {excluded_count} 个下游（上游切断）"
        conn.execute(
            "UPDATE impact_jobs SET status='成功', affected_count=?, excluded_count=?, "
            "message=?, finished_at=datetime('now') WHERE id=?",
            (affected, excluded_count, message, job_id),
        )
        return dict(
            conn.execute(
                "SELECT * FROM impact_jobs WHERE id=?", (job_id,)
            ).fetchone()
        )
