"""上游切断：一次写入，二选一落点（旁路凭证 / 归档物化包）。

规则：
- 同一上游只允许一条生效切断，由唯一索引 + BEGIN IMMEDIATE 串行闸兜底；
- 被切下游只能兑现一条落库命运，不可两套一起开；
- 旁路凭证表收「切断当时」的下游集合，影响分析不得再读；
- 归档物化包把切断指纹封进只读层，指纹与现行边不一致即拦截全量入库。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

from app.config import Config
from app.db import connect, get_db
from app.services.errors import ServiceError

FATE_BYPASS = "旁路凭证"
FATE_ARCHIVE = "归档物化包"
FATES = (FATE_BYPASS, FATE_ARCHIVE)


def _subgraph_edges(conn, upstream_id: int) -> list[list[str]]:
    """切断子图内全部已发布边（含下游之间的边），用于指纹核对。"""
    reachable = {int(upstream_id)}
    frontier = [int(upstream_id)]
    while frontier:
        cur = frontier.pop()
        rows = conn.execute(
            "SELECT downstream_id FROM lineage_edges "
            "WHERE upstream_id=? AND status='已发布'",
            (cur,),
        ).fetchall()
        for r in rows:
            nid = int(r["downstream_id"])
            if nid not in reachable:
                reachable.add(nid)
                frontier.append(nid)
    rows = conn.execute(
        "SELECT e.edge_code, u.node_code AS up_code, d.node_code AS down_code, "
        "e.upstream_id AS uid "
        "FROM lineage_edges e "
        "JOIN lineage_nodes u ON u.id=e.upstream_id "
        "JOIN lineage_nodes d ON d.id=e.downstream_id "
        "WHERE e.status='已发布' ORDER BY e.edge_code"
    ).fetchall()
    return [
        [r["edge_code"], r["up_code"], r["down_code"]]
        for r in rows
        if int(r["uid"]) in reachable
    ]


def fingerprint_of(conn, upstream_id: int) -> str:
    payload = json.dumps(_subgraph_edges(conn, upstream_id), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _downstream_closure(conn, upstream_id: int) -> list[dict]:
    """切断当时，沿已发布边可达的全部下游（含下游的下游）。"""
    seen: dict[int, int] = {}
    order: list[int] = []
    queue = [upstream_id]
    while queue:
        cur = queue.pop(0)
        rows = conn.execute(
            "SELECT downstream_id FROM lineage_edges "
            "WHERE upstream_id=? AND status='已发布' ORDER BY id",
            (cur,),
        ).fetchall()
        for r in rows:
            nid = int(r["downstream_id"])
            if nid not in seen:
                seen[nid] = 1
                order.append(nid)
                queue.append(nid)
    if not order:
        return []
    marks = ",".join("?" * len(order))
    rows = conn.execute(
        f"SELECT id, node_code, table_name, layer FROM lineage_nodes "
        f"WHERE id IN ({marks}) ORDER BY id",
        order,
    ).fetchall()
    by_id = {int(r["id"]): dict(r) for r in rows}
    return [by_id[nid] for nid in order if nid in by_id]


def _active_cut(conn, upstream_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM cut_events WHERE upstream_id=? AND status!='已撤销' ORDER BY id DESC LIMIT 1",
        (upstream_id,),
    ).fetchone()


def list_cuts(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM cut_events ORDER BY id DESC"
        ).fetchall()
        items = []
        for r in rows:
            item = dict(r)
            item["downstream_ids"] = json.loads(r["downstream_ids"] or "[]")
            item["downstream_codes"] = json.loads(r["downstream_codes"] or "[]")
            item["edge_codes"] = json.loads(r["edge_codes"] or "[]")
            if r["fate_kind"] == FATE_BYPASS:
                item["sink_count"] = conn.execute(
                    "SELECT COUNT(*) AS c FROM cut_bypass_vouchers WHERE cut_id=?",
                    (r["id"],),
                ).fetchone()["c"]
                item["sink_label"] = "旁路凭证表"
            else:
                pkg = conn.execute(
                    "SELECT package_no, fingerprint FROM cut_archive_packages WHERE cut_id=?",
                    (r["id"],),
                ).fetchone()
                item["sink_count"] = 1 if pkg else 0
                item["package_no"] = pkg["package_no"] if pkg else ""
                item["sink_label"] = "只读归档层物化包"
            items.append(item)
        return items


def get_cut(cut_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        row = conn.execute("SELECT * FROM cut_events WHERE id=?", (cut_id,)).fetchone()
        if not row:
            raise ServiceError("切断记录不存在", 404)
        item = dict(row)
        item["downstream_ids"] = json.loads(row["downstream_ids"] or "[]")
        item["downstream_codes"] = json.loads(row["downstream_codes"] or "[]")
        item["edge_codes"] = json.loads(row["edge_codes"] or "[]")
        if row["fate_kind"] == FATE_BYPASS:
            item["vouchers"] = [
                dict(v)
                for v in conn.execute(
                    "SELECT * FROM cut_bypass_vouchers WHERE cut_id=? ORDER BY id",
                    (cut_id,),
                ).fetchall()
            ]
        else:
            pkg = conn.execute(
                "SELECT * FROM cut_archive_packages WHERE cut_id=?", (cut_id,)
            ).fetchone()
            item["package"] = dict(pkg) if pkg else None
        return item


def upstream_marks(db_path: str | None = None) -> dict[int, dict]:
    """上游 id → 生效切断标记，供关系页亮「切断来源」。"""
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT id, upstream_id, fate_kind, fingerprint, status, created_at "
            "FROM cut_events WHERE status!='已撤销' ORDER BY id DESC"
        ).fetchall()
        marks: dict[int, dict] = {}
        for r in rows:
            marks.setdefault(
                int(r["upstream_id"]),
                {
                    "cut_id": int(r["id"]),
                    "cut_fate": r["fate_kind"],
                    "cut_fingerprint": r["fingerprint"],
                    "cut_at": r["created_at"],
                },
            )
        return marks


def create_cut(
    upstream_id: int,
    fate_kind: str,
    note: str = "",
    db_path: str | None = None,
) -> dict:
    """服务端单次写入所选落点：切断事件 + 落点内容同一事务。"""
    fate_kind = (fate_kind or "").strip()
    if fate_kind not in FATES:
        raise ServiceError("落点只许选择「旁路凭证」或「归档物化包」其中之一")
    if int(upstream_id or 0) <= 0:
        raise ServiceError("请选择要切断的上游节点")

    path = db_path or Config.database()
    conn = connect(path)
    try:
        # 串行闸：同一上游抢点切断时排队，后到者看到前者的落点
        conn.execute("BEGIN IMMEDIATE")
        up = conn.execute(
            "SELECT * FROM lineage_nodes WHERE id=?", (upstream_id,)
        ).fetchone()
        if not up:
            raise ServiceError("上游节点不存在", 404)
        if up["status"] == "停用":
            raise ServiceError("停用节点不可执行切断", 409)
        if _active_cut(conn, upstream_id):
            raise ServiceError("该上游已有生效切断，不可重复落点", 409)
        # 已被旁路切断剔除的节点不再是活跃分析对象：
        # 再给它落一份命运，会让同一批下游既被剔除又被封存（两套一起开）
        for other in conn.execute(
            "SELECT upstream_code, downstream_ids FROM cut_events "
            "WHERE status='生效' AND fate_kind=?",
            (FATE_BYPASS,),
        ).fetchall():
            if int(upstream_id) in {int(x) for x in json.loads(other["downstream_ids"] or "[]")}:
                raise ServiceError(
                    f"该节点已由上游 {other['upstream_code']} 的旁路切断剔除，"
                    "不能再给它落一份新的切断命运；同一批下游只许兑现一条落库命运",
                    409,
                )

        edges = _subgraph_edges(conn, upstream_id)
        if not edges:
            raise ServiceError("该上游没有已发布出边，无需切断", 409)
        downstreams = _downstream_closure(conn, upstream_id)
        if not downstreams:
            raise ServiceError("该上游没有可达下游，无需切断", 409)

        fp = fingerprint_of(conn, upstream_id)
        d_ids = [int(d["id"]) for d in downstreams]
        d_codes = [d["node_code"] for d in downstreams]
        e_codes = [e[0] for e in edges]

        try:
            cur = conn.execute(
                "INSERT INTO cut_events(upstream_id, upstream_code, fate_kind, fingerprint, "
                "downstream_ids, downstream_codes, edge_codes, sink_ref, status, note) "
                "VALUES (?,?,?,?,?,?,?,'','生效',?)",
                (
                    upstream_id,
                    up["node_code"],
                    fate_kind,
                    fp,
                    json.dumps(d_ids, ensure_ascii=False),
                    json.dumps(d_codes, ensure_ascii=False),
                    json.dumps(e_codes, ensure_ascii=False),
                    note or "",
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ServiceError("并发抢点被串行闸拦下：该上游已有落点，本次未写入") from exc

        cut_id = int(cur.lastrowid)
        if fate_kind == FATE_BYPASS:
            for idx, d in enumerate(downstreams, start=1):
                conn.execute(
                    "INSERT INTO cut_bypass_vouchers(cut_id, upstream_id, downstream_id, "
                    "downstream_code, voucher_no) VALUES (?,?,?,?,?)",
                    (
                        cut_id,
                        upstream_id,
                        int(d["id"]),
                        d["node_code"],
                        f"BV-{cut_id:04d}-{idx:02d}",
                    ),
                )
            sink_ref = f"旁路凭证表 · {len(downstreams)} 条切断快照"
        else:
            payload = json.dumps(
                {
                    "cut_id": cut_id,
                    "upstream_code": up["node_code"],
                    "fingerprint": fp,
                    "edges": edges,
                    "downstream_codes": d_codes,
                    "fate": FATE_ARCHIVE,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            package_no = f"ARC-{cut_id:04d}-{fp[:8]}"
            conn.execute(
                "INSERT INTO cut_archive_packages(cut_id, package_no, fingerprint, payload, sealed_at) "
                "VALUES (?,?,?,?,datetime('now'))",
                (cut_id, package_no, fp, payload),
            )
            sink_ref = f"只读归档层物化包 · {package_no}"

        conn.execute(
            "UPDATE cut_events SET sink_ref=? WHERE id=?", (sink_ref, cut_id)
        )
        conn.commit()
    except ServiceError:
        conn.rollback()
        raise
    except Exception as exc:  # 写败必须中文说明
        conn.rollback()
        raise ServiceError(f"切断写入失败，未落任何落点：{exc}", 500) from exc
    finally:
        conn.close()

    return get_cut(cut_id, db_path=db_path)


def bypassed_node_ids(db_path: str | None = None) -> dict[int, int]:
    """旁路切断剔除的节点 → 所属切断 id。归档落点不剔除下游。"""
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT id, downstream_ids FROM cut_events "
            "WHERE status='生效' AND fate_kind=?",
            (FATE_BYPASS,),
        ).fetchall()
        out: dict[int, int] = {}
        for r in rows:
            for nid in json.loads(r["downstream_ids"] or "[]"):
                out.setdefault(int(nid), int(r["id"]))
        return out


def verify_fingerprints(db_path: str | None = None) -> list[dict]:
    """核对每份生效切断的指纹与现行边是否一致。"""
    results = []
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM cut_events WHERE status='生效' ORDER BY id"
        ).fetchall()
        for r in rows:
            current = fingerprint_of(conn, int(r["upstream_id"]))
            results.append(
                {
                    "cut_id": int(r["id"]),
                    "upstream_code": r["upstream_code"],
                    "fate_kind": r["fate_kind"],
                    "fingerprint": r["fingerprint"],
                    "current_fingerprint": current,
                    "consistent": current == r["fingerprint"],
                }
            )
    return results
