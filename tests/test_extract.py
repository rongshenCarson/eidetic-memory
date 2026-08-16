"""提炼插件测试：mock extractor 验证入库链路（decisions→extracts / facts→KG）"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory_server import db
from memory_server.extract import extract_and_store
from memory_server.kg import kg_query


class MockExtractor:
    name = "mock"
    def extract(self, text):
        return {
            "facts": [
                {"type": "decision", "content": "决定采用双轨方案", "short": "双轨方案",
                 "importance": 4, "entities": ["林下有"], "timestamp": "2026-08-09"},
                {"type": "fact", "content": "云茯苓祛湿茶是首款产品", "short": "首款产品",
                 "importance": 3, "entities": ["林下有", "云茯苓祛湿茶"], "timestamp": "2026-08-09"},
            ],
            "episodics": [
                {"scene": "推广方案讨论", "summary": "讨论了双轨推广方案并定稿",
                 "short": "双轨推广定稿", "importance": 4, "timestamp": "2026-08-09"},
            ],
        }


def test_extract_store_to_kg():
    db.init_db()
    res = extract_and_store("测试文本", namespace="brand", extractor=MockExtractor())
    assert res["facts"] == 2 and res["episodics"] == 1

    # extracts 断言（含新字段）
    conn = db.get_conn()
    rows = conn.execute("SELECT type, text, short, importance FROM extracts ORDER BY type").fetchall()
    conn.close()
    types = [r[0] for r in rows]
    assert types == ["decision", "episodic", "fact"]
    assert any(r[0] == "decision" and r[2] == "双轨方案" and r[3] == 4 for r in rows)

    # KG 实体关联断言（实体 → 涉及 → 内容）
    from memory_server.kg import kg_query
    out = kg_query("林下有", namespace="brand", direction="outgoing")
    assert out["count"] >= 1


def test_extract_none_returns_none():
    db.init_db()
    class NoneExt:
        name = "none"
        def extract(self, text):
            return {}
    assert extract_and_store("x", "t", NoneExt()) is None
