"""覆盖对账与全量入库。

覆盖页的抽检核对三件事：
1. 切断标记（每份生效切断的指纹与现行边一致）；
2. 剔除条数（影响作业的剔除行与切断快照对得上）；
3. 表内命运行（impact_hits / cut_bypass_vouchers / cut_archive_packages 的真实行数）。

全量入库前先核指纹：任一生效切断的指纹与现行边不一致即拦截，不写库。
"""
from __future__ import annotations

import json

from app.db import get_db
from app.services import cuts as cuts_svc
from app.services.errors import ServiceError


def list_reports(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM coverage_reports ORDER BY id DESC"
        ).fetchall()
        return [_report_dict(r) for r in rows]


def _report_dict(row) -> dict:
    item = dict(row)
    item["checks"] = json.loads(row["checks_json"] or "[]")
    return item


def _check(conn) -> tuple[list[dict], dict]:
    checks: list[dict] = []

    # 指纹核对（用当前连接直接算，避免另开连接读到未提交数据）
    fp_rows = conn.execute(
        "SELECT * FROM cut_events WHERE status='生效' ORDER BY id"
    ).fetchall()
    mismatched = []
    for r in fp_rows:
        current = cuts_svc.fingerprint_of(conn, int(r["upstream_id"]))
        if current != r["fingerprint"]:
            mismatched.append(
                {"cut_id": int(r["id"]), "upstream_code": r["upstream_code"],
                 "expected": r["fingerprint"], "current": current}
            )
    checks.append(
        {
            "key": "cut_marks",
            "label": "切断标记",
            "passed": not mismatched,
            "detail": (
                f"生效切断 {len(fp_rows)} 份，指纹全部一致"
                if not mismatched
                else "指纹不一致：" + "、".join(x["upstream_code"] for x in mismatched)
            ),
            "mismatched": mismatched,
        }
    )

    # 剔除条数：影响作业记录的剔除行 vs 切断快照里确有、但未进命中集的节点
    job_rows = conn.execute(
        "SELECT id, job_code, excluded_count FROM impact_jobs WHERE status='成功' ORDER BY id"
    ).fetchall()
    exclusion_mismatch = []
    for j in job_rows:
        actual = conn.execute(
            "SELECT COUNT(*) AS c FROM impact_exclusions WHERE job_id=?", (j["id"],)
        ).fetchone()["c"]
        if int(actual) != int(j["excluded_count"]):
            exclusion_mismatch.append(
                {"job_code": j["job_code"], "recorded": int(j["excluded_count"]), "actual": int(actual)}
            )
    checks.append(
        {
            "key": "excluded_rows",
            "label": "剔除条数",
            "passed": not exclusion_mismatch,
            "detail": (
                f"{len(job_rows)} 个成功作业，剔除行与记录一致"
                if not exclusion_mismatch
                else "剔除行不一致：" + "、".join(x["job_code"] for x in exclusion_mismatch)
            ),
            "mismatched": exclusion_mismatch,
        }
    )

    # 表内命运行：切断与命中的表必须真的存在行
    cut_total = conn.execute("SELECT COUNT(*) AS c FROM cut_events").fetchone()["c"]
    voucher_rows = conn.execute(
        "SELECT COUNT(*) AS c FROM cut_bypass_vouchers"
    ).fetchone()["c"]
    archive_rows = conn.execute(
        "SELECT COUNT(*) AS c FROM cut_archive_packages"
    ).fetchone()["c"]
    hit_rows = conn.execute("SELECT COUNT(*) AS c FROM impact_hits").fetchone()["c"]
    exclusion_rows = conn.execute(
        "SELECT COUNT(*) AS c FROM impact_exclusions"
    ).fetchone()["c"]

    bypass_cuts = sum(
        len(json.loads(r["downstream_ids"] or "[]"))
        for r in conn.execute(
            "SELECT downstream_ids FROM cut_events "
            "WHERE status!='已撤销' AND fate_kind='旁路凭证'"
        ).fetchall()
    )
    archive_cuts = conn.execute(
        "SELECT COUNT(*) AS c FROM cut_events WHERE status!='已撤销' AND fate_kind='归档物化包'"
    ).fetchone()["c"]
    table_ok = int(voucher_rows) == int(bypass_cuts) and int(archive_rows) == int(archive_cuts)
    checks.append(
        {
            "key": "table_rows",
            "label": "表内命运行",
            "passed": table_ok,
            "detail": (
                f"切断 {cut_total} · 旁路凭证 {voucher_rows} · 归档包 {archive_rows} · "
                f"命中行 {hit_rows} · 剔除行 {exclusion_rows}"
            ),
            "counts": {
                "cut_events": int(cut_total),
                "bypass_vouchers": int(voucher_rows),
                "archive_packages": int(archive_rows),
                "impact_hits": int(hit_rows),
                "impact_exclusions": int(exclusion_rows),
            },
        }
    )
    return checks, {
        "cut_count": int(cut_total),
        "hit_rows": int(hit_rows),
        "exclusion_rows": int(exclusion_rows),
    }


def run_coverage(db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        nodes = conn.execute("SELECT id, status FROM lineage_nodes").fetchall()
        edges = conn.execute(
            "SELECT id, upstream_id, downstream_id, status FROM lineage_edges"
        ).fetchall()
        live_nodes = [n for n in nodes if n["status"] != "停用"]
        published = [e for e in edges if e["status"] == "已发布"]
        linked = set()
        for e in published:
            linked.add(int(e["upstream_id"]))
            linked.add(int(e["downstream_id"]))
        orphan = sum(1 for n in live_nodes if int(n["id"]) not in linked)
        uncovered = sum(1 for e in edges if e["status"] == "草稿")

        excluded_total = conn.execute(
            "SELECT COUNT(*) AS c FROM impact_exclusions"
        ).fetchone()["c"]

        checks, extra = _check(conn)
        passed = sum(1 for c in checks if c["passed"])
        total = len(checks)
        verdict = "抽检通过" if passed == total else "抽检未过"

        parts = [
            f"在用节点 {len(live_nodes)}",
            f"已发布边 {len(published)}",
            f"孤儿节点 {orphan}",
            f"草稿边 {uncovered}",
            f"生效切断 {extra['cut_count']}",
            f"剔除条数 {int(excluded_total)}",
            f"命中行 {extra['hit_rows']}",
            f"抽检 {passed}/{total} {verdict}",
        ]
        summary = "，".join(parts)
        if orphan:
            summary += "；存在未挂边节点须人工核对"

        cur = conn.execute(
            "INSERT INTO coverage_reports(summary, node_count, edge_count, orphan_count, "
            "uncovered_edges, cut_count, excluded_count, check_total, check_passed, "
            "verdict, checks_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                summary,
                len(live_nodes),
                len(published),
                orphan,
                uncovered,
                extra["cut_count"],
                int(excluded_total),
                total,
                passed,
                verdict,
                json.dumps(checks, ensure_ascii=False),
            ),
        )
        return _report_dict(
            conn.execute(
                "SELECT * FROM coverage_reports WHERE id=?", (cur.lastrowid,)
            ).fetchone()
        )


def run_ingest(db_path: str | None = None) -> dict:
    """全量采集入库：先核切断指纹，不一致即拦截且不写业务表。"""
    from app.db import connect

    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM cut_events WHERE status='生效' ORDER BY id"
        ).fetchall()
        mismatched = []
        for r in rows:
            current = cuts_svc.fingerprint_of(conn, int(r["upstream_id"]))
            if current != r["fingerprint"]:
                mismatched.append(
                    f"{r['upstream_code']}（切断指纹 {r['fingerprint']} ≠ 现行边 {current}）"
                )
        if mismatched:
            message = "全量入库已拦截：切断指纹与现行边不一致 —— " + "；".join(mismatched)
            conn.rollback()
            # 拦截留痕单独提交，保证覆盖页看得到「未入库」
            audit = connect(db_path)
            try:
                audit.execute(
                    "INSERT INTO ingest_runs(mode, status, message, node_count, edge_count, cut_count) "
                    "VALUES ('全量','拦截',?,0,0,?)",
                    (message, len(rows)),
                )
                audit.commit()
            finally:
                audit.close()
            raise ServiceError(message, 409)
        node_count = conn.execute("SELECT COUNT(*) AS c FROM lineage_nodes").fetchone()["c"]
        edge_count = conn.execute(
            "SELECT COUNT(*) AS c FROM lineage_edges WHERE status='已发布'"
        ).fetchone()["c"]
        message = (
            f"全量入库完成：节点 {node_count}、已发布边 {edge_count}，"
            f"生效切断 {len(rows)} 份指纹一致"
        )
        cur = conn.execute(
            "INSERT INTO ingest_runs(mode, status, message, node_count, edge_count, cut_count) "
            "VALUES ('全量','成功',?,?,?,?)",
            (message, node_count, edge_count, len(rows)),
        )
        return _run_dict(
            conn.execute("SELECT * FROM ingest_runs WHERE id=?", (cur.lastrowid,)).fetchone()
        )


def _run_dict(row) -> dict:
    return dict(row)


def list_ingests(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM ingest_runs ORDER BY id DESC"
        ).fetchall()
        return [_run_dict(r) for r in rows]


def latest_chip(db_path: str | None = None) -> str:
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT orphan_count, verdict FROM coverage_reports ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return "未对账"
        if row["verdict"] and row["verdict"] != "抽检通过":
            return "抽检未过"
        return "有缺口" if int(row["orphan_count"]) > 0 else "已齐套"
