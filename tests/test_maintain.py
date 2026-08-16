"""维护模块测试（F5）：LRN/curated/冲突/反馈/反思"""
import sys, os, time, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from memory_server import db
from memory_server import maintain


@pytest.fixture(autouse=True)
def seed(clean_db):
    conn = db.get_conn()
    t = time.time()
    for i, txt in enumerate([
        "用户：不对，这个方案完全搞错了，重来",
        "用户：这个总结做得很好，方向完全正确",
        "brandx定稿了双轨推广方案",
    ]):
        conn.execute(
            "INSERT INTO chunks(source_id, namespace, path, start_line, end_line, text, "
            "embedding, updated_at, metadata) VALUES(?,?,?,?,?,?,?,?,?)",
            (i, "main", "p", 0, 1, txt, None, t, "{}"))
    conn.execute(
        "INSERT INTO extracts(namespace, type, text, short, importance, entities, "
        "tags, timestamp, source_id, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("main", "decision", "双轨推广方案定稿", "双轨方案定稿", 5, '["acme"]',
         '[]', "2026-08-09", 1, t))
    conn.commit()
    conn.close()


def test_lrn_management():
    r = maintain.add_learning("main", "测试教训", root_cause="根因", lesson="内容")
    assert r["code"].startswith("LRN-")
    rs = maintain.list_learnings(namespace="main", keyword="测试")
    assert rs and rs[0]["code"] == r["code"]


def test_sync_curated():
    r = maintain.sync_curated(namespace="main")
    assert r and r["items"] >= 1
    conn = db.get_conn()
    cur = conn.execute("SELECT content FROM curated").fetchone()
    conn.close()
    assert cur and "双轨方案定稿" in cur[0]


def test_conflict_scan():
    conn = db.get_conn()
    t = time.time()
    conn.execute("INSERT INTO triples(namespace, subject, predicate, object, valid_from, valid_to, source_id, created_at) VALUES(?,?,?,?,?,?,?,?)",
                 ("main", "X", "rel", "v1", "2026-01-01", None, None, t))
    conn.execute("INSERT INTO triples(namespace, subject, predicate, object, valid_from, valid_to, source_id, created_at) VALUES(?,?,?,?,?,?,?,?)",
                 ("main", "X", "rel", "v2", "2026-06-01", None, None, t))
    conn.commit()
    conn.close()
    r = maintain.scan_conflicts(namespace="main")
    assert r["count"] >= 1 and r["conflicts"][0]["subject"] == "X"


def test_feedback_signals(tmp_path):
    sf = str(tmp_path / "state.json")
    r = maintain.detect_feedback(namespace="main", window_h=48, state_file=sf)
    assert r["negative"] >= 1 and r["positive"] >= 1
    assert len(r["learnings_written"]) >= 1
    r2 = maintain.detect_feedback(namespace="main", window_h=48, state_file=sf)
    assert r2["negative"] == 0  # 幂等


def test_reflection(monkeypatch):
    from memory_server import pipeline
    monkeypatch.setattr(pipeline, "_call_llm", lambda ex, p: "反思内容")
    class MockExt:
        name = "mock"
    r = maintain.run_reflection(MockExt(), namespace="main")
    assert r["status"] == "ok"
    r2 = maintain.run_reflection(MockExt(), namespace="main")
    assert r2["status"] == "skip"  # 同日幂等
