"""上游切断、二选一落点、剔除条数与全量入库拦截的回归测试。"""
import json
import os
import sqlite3
import threading

import pytest

from app.services import coverage, cuts, impact, nodes
from app.services.cuts import FATE_ARCHIVE, FATE_BYPASS
from app.services.errors import ServiceError


def _node_id(db_path, code):
    return int(next(n for n in nodes.list_nodes(db_path=db_path) if n["node_code"] == code)["id"])


def test_fingerprint_matches_current_edges(db_path):
    up = _node_id(db_path, "N-DWD-ORD")
    info = cuts.verify_fingerprints(db_path=db_path)
    assert info == []  # 尚无切断
    result = cuts.create_cut(up, FATE_BYPASS, db_path=db_path)
    assert result["status"] == "生效"
    assert result["edge_codes"] == ["E-03", "E-05", "E-06"]
    assert result["downstream_codes"]
    checks = cuts.verify_fingerprints(db_path=db_path)
    assert checks[0]["consistent"] is True


def test_bypass_fate_writes_snapshot_and_excludes_from_impact(db_path):
    up = _node_id(db_path, "N-DWD-ORD")
    cut = cuts.create_cut(up, FATE_BYPASS, db_path=db_path)
    assert cut["fate_kind"] == FATE_BYPASS
    assert cut["vouchers"] and len(cut["vouchers"]) == len(cut["downstream_ids"])
    # 旁路凭证收的是切断当时的下游集合
    assert set(cut["downstream_codes"]) >= {"N-DWS-REV", "N-ADS-DASH", "N-ADS-ALERT"}

    root = _node_id(db_path, "N-DWD-ORD")
    job = impact.start_impact(root, db_path=db_path)
    detail = impact.job_detail(int(job["id"]), db_path=db_path)
    hit_codes = {h["node_code"] for h in detail["hits"]}
    assert "N-DWS-REV" not in hit_codes
    assert detail["excluded_count"] == len(cut["downstream_ids"])
    assert {x["node_code"] for x in detail["excluded"]} == set(cut["downstream_codes"])

    # 被旁路剔除的节点不得自行按旧路径再开作业
    with pytest.raises(ServiceError) as exc:
        impact.start_impact(_node_id(db_path, "N-DWS-REV"), db_path=db_path)
    assert "旁路切断剔除" in str(exc.value)


def test_archive_fate_keeps_legacy_path(db_path):
    up = _node_id(db_path, "N-DWS-REV")
    cut = cuts.create_cut(up, FATE_ARCHIVE, db_path=db_path)
    assert cut["package"]["fingerprint"] == cut["fingerprint"]
    # 归档包只读
    conn = sqlite3.connect(db_path)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("UPDATE cut_archive_packages SET payload='x'")
        conn.rollback()
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM cut_archive_packages")
        conn.rollback()
    finally:
        conn.close()
    root = _node_id(db_path, "N-DWS-REV")
    job = impact.start_impact(root, db_path=db_path)
    detail = impact.job_detail(int(job["id"]), db_path=db_path)
    assert detail["excluded_count"] == 0
    assert {h["node_code"] for h in detail["hits"]} >= {"N-ADS-DASH", "N-ADS-ALERT"}
    # 归档落点不改路径，影响页照旧带下游
    assert job["affected_count"] == 3


def test_one_upstream_one_fate_only(db_path):
    up = _node_id(db_path, "N-DWS-REV")
    cuts.create_cut(up, FATE_BYPASS, db_path=db_path)
    with pytest.raises(ServiceError) as exc:
        cuts.create_cut(up, FATE_ARCHIVE, db_path=db_path)
    assert "已有生效切断" in str(exc.value)


def test_bad_fate_rejected(db_path):
    up = _node_id(db_path, "N-DWS-REV")
    with pytest.raises(ServiceError) as exc:
        cuts.create_cut(up, "两套一起开", db_path=db_path)
    assert "落点" in str(exc.value)


def test_each_cut_commits_exactly_one_fate(db_path):
    """一份切断只写一个落点：旁路与归档的落库行互斥。"""
    cut = cuts.create_cut(_node_id(db_path, "N-DWS-REV"), FATE_BYPASS, db_path=db_path)
    assert cut["vouchers"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    archive = conn.execute(
        "SELECT COUNT(*) c FROM cut_archive_packages WHERE cut_id=?", (cut["id"],)
    ).fetchone()["c"]
    conn.close()
    assert archive == 0


def test_bypassed_node_cannot_carry_a_second_fate(db_path):
    """同一上游不能落第二份命运（串行闸+唯一索引），先到者生效。"""
    up = _node_id(db_path, "N-DWD-ORD")
    cuts.create_cut(up, FATE_BYPASS, db_path=db_path)
    with pytest.raises(ServiceError) as exc:
        cuts.create_cut(up, FATE_ARCHIVE, db_path=db_path)
    assert "已有生效切断" in str(exc.value)
    assert len(cuts.list_cuts(db_path=db_path)) == 1


def test_bypass_removed_node_cannot_open_new_cut(db_path):
    """被旁路剔除的下游不能再当切断上游，否则同批下游两套命运齐开。"""
    cuts.create_cut(_node_id(db_path, "N-DWD-ORD"), FATE_BYPASS, db_path=db_path)
    with pytest.raises(ServiceError) as exc:
        cuts.create_cut(_node_id(db_path, "N-DWS-REV"), FATE_ARCHIVE, db_path=db_path)
    assert "只能兑现一条落库命运" in str(exc.value) or "落一份新的切断命运" in str(exc.value)
    assert len(cuts.list_cuts(db_path=db_path)) == 1


def test_ingest_blocked_when_cut_fingerprint_drifts(db_path):
    up = _node_id(db_path, "N-DWS-REV")
    cuts.create_cut(up, FATE_BYPASS, db_path=db_path)
    ok = coverage.run_ingest(db_path=db_path)
    assert ok["status"] == "成功"

    # 现行边变动：该上游多出一条已发布出边，指纹随即漂移
    extra = nodes.register_node("N-ADS-AUD", "ads_audit_rev", "ADS", "值班组", db_path=db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO lineage_edges(edge_code, upstream_id, downstream_id, transform_kind, status, note) "
        "VALUES ('E-NEW-9',?,?,'直抽','已发布','')",
        (up, int(extra["id"])),
    )
    conn.commit()
    conn.close()

    with pytest.raises(ServiceError) as exc:
        coverage.run_ingest(db_path=db_path)
    assert "拦截" in str(exc.value)
    runs = coverage.list_ingests(db_path=db_path)
    assert runs[0]["status"] == "拦截"


def test_coverage_checks_mark_exclusion_and_rows(db_path):
    up = _node_id(db_path, "N-DWD-ORD")
    cuts.create_cut(up, FATE_BYPASS, db_path=db_path)
    impact.start_impact(_node_id(db_path, "N-DWD-ORD"), db_path=db_path)
    rep = coverage.run_coverage(db_path=db_path)
    assert rep["verdict"] == "抽检通过"
    keys = [c["key"] for c in rep["checks"]]
    assert keys == ["cut_marks", "excluded_rows", "table_rows"]
    assert rep["check_passed"] == rep["check_total"] == 3
    assert rep["excluded_count"] > 0


def test_concurrent_cut_serialized(db_path):
    up = _node_id(db_path, "N-DWS-REV")
    results = []
    errors = []

    def worker(fate):
        try:
            results.append(cuts.create_cut(up, fate, db_path=db_path))
        except ServiceError as exc:
            errors.append(str(exc))

    t1 = threading.Thread(target=worker, args=(FATE_BYPASS,))
    t2 = threading.Thread(target=worker, args=(FATE_ARCHIVE,))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert len(results) == 1 and len(errors) == 1
    # 表内恰一份命运行，且落点只有一列
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cut_rows = conn.execute("SELECT * FROM cut_events").fetchall()
    bypass = conn.execute("SELECT COUNT(*) c FROM cut_bypass_vouchers").fetchone()["c"]
    archive = conn.execute("SELECT COUNT(*) c FROM cut_archive_packages").fetchone()["c"]
    conn.close()
    assert len(cut_rows) == 1
    assert (bypass == 0) != (archive == 0)  # 不可两套一起开
