"""融合检索测试（F4）：KG 多跳 / 融合召回 / 任务偏置 / room 过滤 / 时间衰减"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from memory_server import db
from memory_server.embed import FtsOnlyProvider
from memory_server.kg import kg_add


@pytest.fixture(autouse=True)
def seed(clean_db):
    conn = db.get_conn()
    t = time.time()
    for txt, sid in [
        ("讨论teaprod的成分和功效", 1),
        ("teaprod的口感和配方细节", 4),
        ("brandx的推广方案定了双轨策略", 2),
        ("今天天气不错去公园散步", 3),
    ]:
        conn.execute(
            "INSERT INTO chunks(source_id, namespace, path, start_line, end_line, text, "
            "embedding, updated_at, metadata) VALUES(?,?,?,?,?,?,?,?,?)",
            (sid, "brand", "p", 0, 1, txt, None, t, "{}"))
    conn.commit()
    conn.close()
    kg_add("acme", "首款产品", "teaprod", namespace="brand")
    from memory_server.classifier import auto_classify
    auto_classify(namespace="brand")


def test_kg_entity_expand_multi_hop():
    from memory_server.search import _kg_entity_expand
    kg = _kg_entity_expand("acme", "brand", depth=2)
    assert "teaprod" in kg["entities"]
    assert len(kg["chunk_ids"]) >= 2


def test_fusion_recalls_kg_related():
    from memory_server.search import fusion_search
    r = fusion_search("acme 推广", namespace="brand", limit=3,
                      provider=FtsOnlyProvider(), embed=False)
    assert any("双轨策略" in x["text"] for x in r["results"])


def test_task_context_boost():
    from memory_server.search import fusion_search
    r = fusion_search("推广", namespace="brand", limit=3,
                      provider=FtsOnlyProvider(), embed=False, task_context="brandx")
    assert "双轨策略" in r["results"][0]["text"]


def test_room_filter():
    from memory_server.search import fusion_search
    r = fusion_search("内容", namespace="brand", limit=5,
                      provider=FtsOnlyProvider(), embed=False, room="品牌推广")
    assert r["results"] and "公园散步" not in r["results"][0]["text"]


def test_time_decay():
    from memory_server.search import fusion_search
    conn = db.get_conn()
    old_t = time.time() - 90 * 86400
    conn.execute(
        "INSERT INTO chunks(source_id, namespace, path, start_line, end_line, text, "
        "embedding, updated_at, metadata) VALUES(?,?,?,?,?,?,?,?,?)",
        (9, "brand", "p", 0, 1, "90天前讨论的acme推广老方案", None, old_t, "{}"))
    conn.commit()
    conn.close()
    r = fusion_search("brandx 推广", namespace="brand", limit=5,
                      provider=FtsOnlyProvider(), embed=False)
    assert "90天前" not in r["results"][0]["text"]
