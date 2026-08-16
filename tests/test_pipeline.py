"""完整提炼管道测试（F2）：L2 场景 / L3 画像 / 核心记忆晋升"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from memory_server import db
from memory_server import pipeline


class MockExt:
    name = "mock"


@pytest.fixture(autouse=True)
def seed(clean_db):
    conn = db.get_conn()
    t = time.time()
    for i in range(8):
        conn.execute(
            "INSERT INTO chunks(source_id, namespace, path, start_line, end_line, text, "
            "embedding, updated_at, metadata) VALUES(?,?,?,?,?,?,?,?,?)",
            (1, "brand", "p", 0, 1,
             f"林下有品牌云茯苓祛湿茶推广方案讨论第{i}轮", None, t, "{}"))
    for i in range(3):
        conn.execute(
            "INSERT INTO extracts(namespace, type, text, short, importance, entities, "
            "tags, timestamp, source_id, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("brand", "decision", f"决策{i}", "短版", 4, '["林下有"]', '[]',
             "2026-08-09", 1, t))
    conn.commit()
    conn.close()


def test_promote_keywords():
    r = pipeline.run_promote(namespace="brand", min_freq=3)
    assert r and r["added"] >= 1
    conn = db.get_conn()
    rows = conn.execute("SELECT keyword, freq FROM core_memories ORDER BY freq DESC LIMIT 3").fetchall()
    conn.close()
    assert rows and rows[0]["freq"] >= 3


def test_l2_scenes(monkeypatch):
    monkeypatch.setattr(pipeline, "_call_llm",
                        lambda ex, p: "## 当前活跃场景\n- **推广**: 进行中")
    r = pipeline.run_l2_scenes(MockExt(), namespace="brand", window_h=48)
    assert r and r["chars"] > 0
    conn = db.get_conn()
    scenes = conn.execute("SELECT namespace, status FROM scenes").fetchall()
    conn.close()
    assert len(scenes) == 1 and scenes[0]["namespace"] == "brand"


def test_l3_persona(monkeypatch):
    monkeypatch.setattr(pipeline, "_call_llm",
                        lambda ex, p: "## 当前角色\n- 创业者\n\n## 长期偏好\n- 高效")
    # 先有场景
    pipeline.run_l2_scenes(MockExt(), namespace="brand", window_h=48)
    r = pipeline.run_l3_persona(MockExt(), namespace="brand")
    assert r and r["namespace"] == "brand"
    conn = db.get_conn()
    persona = conn.execute("SELECT content FROM persona WHERE namespace='brand'").fetchone()
    conn.close()
    assert persona and "创业者" in persona["content"]


def test_l3_without_scenes_returns_none(monkeypatch):
    r = pipeline.run_l3_persona(MockExt(), namespace="empty_ns")
    assert r is None
