"""KG 插件测试：写入/查询/幂等/冲突修正/时间线/统计"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory_server import db
from memory_server.kg import kg_add, kg_query, kg_stats, kg_timeline


def setup_function():
    db.init_db()


def test_add_and_query_both_directions():
    kg_add("林下有", "首款产品", "云茯苓祛湿茶", namespace="brand")
    out = kg_query("林下有", namespace="brand", direction="outgoing")
    assert out["count"] == 1 and out["relations"][0]["object"] == "云茯苓祛湿茶"
    inc = kg_query("云茯苓祛湿茶", namespace="brand", direction="incoming")
    assert inc["count"] == 1 and inc["relations"][0]["object"] == "林下有"
    both = kg_query("云茯苓祛湿茶", namespace="brand", direction="both")
    assert both["count"] == 1


def test_idempotent():
    kg_add("A", "r", "B", namespace="t")
    r = kg_add("A", "r", "B", namespace="t")
    assert r["status"] == "duplicate"
    assert kg_stats(namespace="t")["triples"] == 1


def test_conflict_correction():
    kg_add("林下有", "首款产品", "云茯苓祛湿茶", namespace="brand")
    kg_add("林下有", "首款产品", "新品A", namespace="brand")
    s = kg_stats(namespace="brand")
    assert s["triples"] == 2 and s["expired"] == 1
    # 旧事实被标记过时
    out = kg_query("林下有", namespace="brand", direction="outgoing")
    vt = {r["object"]: r["valid_to"] for r in out["relations"]}
    assert vt["云茯苓祛湿茶"] is not None  # 已过时
    assert vt["新品A"] is None  # 当前有效


def test_as_of_time_filter():
    kg_add("X", "rel", "Y", namespace="t", valid_from="2026-01-01")
    kg_add("X", "rel", "Z", namespace="t", valid_from="2026-06-01")
    before = kg_query("X", namespace="t", direction="outgoing", as_of="2026-03-01")
    after = kg_query("X", namespace="t", direction="outgoing", as_of="2026-09-01")
    # 3 月时只有 Y；9 月时 Y 已被冲突修正标记过时（valid_to=06-01），只剩 Z
    assert before["count"] == 1 and before["relations"][0]["object"] == "Y"
    assert after["count"] == 1 and after["relations"][0]["object"] == "Z"


def test_stats_and_timeline():
    kg_add("A", "r1", "B", namespace="t")
    kg_add("B", "r2", "C", namespace="t")
    s = kg_stats(namespace="t")
    assert s["entities"] == 3 and s["triples"] == 2
    tl = kg_timeline("A", namespace="t")
    assert len(tl) == 1 and tl[0]["predicate"] == "r1"


def test_entity_normalization_short_to_long():
    """短名归入长名（Mem0 entity linking 借鉴 ①）"""
    kg_add("林下有品牌", "定位", "轻养生药食同源", namespace="brand")
    r = kg_add("林下有", "首款产品", "云茯苓祛湿茶", namespace="brand")
    assert r["subject"] == "林下有品牌"
    q = kg_query("林下有", namespace="brand", direction="outgoing")
    assert q["count"] == 2  # 别名查询命中全部


def test_entity_normalization_long_takes_over():
    """长名接管 canonical，triples 连坐更新"""
    kg_add("测试实体A", "关系", "对象B", namespace="t")
    kg_add("测试实体A完整", "关系2", "对象C", namespace="t")
    q = kg_query("测试实体A", namespace="t", direction="outgoing")
    assert q["count"] == 2
    preds = {x["predicate"] for x in q["relations"]}
    assert preds == {"关系", "关系2"}
