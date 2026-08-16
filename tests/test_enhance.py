"""借鉴增强测试（②a-d ③a-b）：语义去重/AAAK/wake_up/L1决策/导出/实体摘要"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from memory_server import db


@pytest.fixture(autouse=True)
def seed(clean_db):
    conn = db.get_conn()
    t = time.time()
    texts = [
        "brandx定稿了双轨推广方案，influencer focus 5k-10k",
        "brandx的推广方案采用双轨策略，influencer 5k-10k range",
        "今天天气很好去公园散步了",
    ]
    for i, txt in enumerate(texts):
        conn.execute(
            "INSERT INTO chunks(source_id, namespace, path, start_line, end_line, text, "
            "embedding, updated_at, metadata) VALUES(?,?,?,?,?,?,?,?,?)",
            (i, "main", "p", 0, 1, txt, None, t, "{}"))
    conn.commit()
    conn.close()


def test_semantic_dedup():
    """②a 语义去重：相似对识别（需向量）"""
    from memory_server.dedup import _grouped_rows
    groups = _grouped_rows(namespace="main")
    assert "main" in groups and len(groups["main"]) == 3


def test_aaak_compress():
    """②b AAAK 压缩：结构化字段 + 幂等"""
    from memory_server.compress import compress_text, compress_chunks
    c = compress_text("the user确认 Eidetic 定名，这是重要决策。")
    assert c["topic"] and c["key_sentence"]
    assert "decision" in c["flags"] or "milestone" in c["flags"]
    r = compress_chunks(namespace="main")
    assert r["generated"] >= 1
    r2 = compress_chunks(namespace="main")
    assert r2["generated"] == 0  # 幂等


def test_wakeup():
    """②c wake_up：L0+L1 组合 + token 上限"""
    from memory_server.wakeup import wake_up
    conn = db.get_conn()
    t = time.time()
    conn.execute("INSERT INTO persona(namespace, content, updated_at) VALUES(?,?,?)",
                 ("main", "## 当前角色\n- 测试用户", t))
    conn.execute("INSERT INTO curated(namespace, topic, content, updated_at) VALUES(?,?,?,?)",
                 ("main", "core_summary", "- [decision] 测试决策", t))
    conn.commit()
    conn.close()
    r = wake_up(namespace="main", max_tokens=300)
    assert 0 < r["tokens"] <= 300
    assert "L0" in r["text"] and "L1" in r["text"]


def test_l1_noop_decision():
    """②d L1 更新决策：相似表述不重复累积"""
    from memory_server.extract import extract_and_store
    class MockExt:
        name = "mock"
        def extract(self, text):
            return {"facts": [{"type": "decision", "content": text, "short": text[:20],
                               "importance": 4, "entities": [], "timestamp": "2026-08-10"}],
                    "episodics": []}
    t1 = "the user决定采用双轨推广方案，influencer 5k-10k range"
    extract_and_store(t1, "main", MockExt())
    extract_and_store(t1, "main", MockExt())  # 相同 → NOOP
    conn = db.get_conn()
    n = conn.execute("SELECT COUNT(*) FROM extracts WHERE type='decision'").fetchone()[0]
    conn.close()
    assert n == 1


def test_markdown_export(tmp_path):
    """③a Markdown 导出：人机可读文件"""
    from memory_server.export_md import export_markdown
    out = str(tmp_path / "exp")
    r = export_markdown(out, namespace="main")
    assert r["namespaces"] == 1 and r["files"] >= 2
    import glob
    files = glob.glob(os.path.join(out, "main", "*.md"))
    assert files
    content = open(files[0]).read()
    assert content  # 非空可读


def test_entity_summary():
    """③b 实体摘要：规则兜底落库"""
    from memory_server.kg import kg_add, entity_summary
    kg_add("acme", "首款产品", "teaprod", namespace="main")
    class NoneExt:
        name = "none"
    r = entity_summary(NoneExt(), namespace="main", top_n=5)
    assert r["summarized"] >= 1
    conn = db.get_conn()
    row = conn.execute("SELECT summary FROM entities WHERE namespace='main' "
                       "AND canonical='acme'").fetchone()
    conn.close()
    assert row and row[0] and "首款产品" in row[0]
