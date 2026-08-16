"""pytest 会话级隔离：独立临时数据库 + 每测试前清库"""
import os
import tempfile

# 必须在 import memory_server.db 之前设置（DB_PATH 是模块级常量）
os.environ["MEMORY_SERVER_DB_DIR"] = tempfile.mkdtemp(prefix="ms_test_")
# 2026-08-10: RAW_DIR 也隔离（否则测试写真实 raw/ 污染原始层）
os.environ["MEMORY_SERVER_RAW_DIR"] = tempfile.mkdtemp(prefix="ms_test_raw_")

import pytest


@pytest.fixture(autouse=True)
def clean_db():
    """每个测试前清空数据表，保证断言从空库开始"""
    from memory_server import db
    db.init_db()
    conn = db.get_conn()
    for t in ("triples", "entities", "extracts", "sources", "chunks",
              "ingested_hashes", "watermarks"):
        try:
            conn.execute(f"DELETE FROM {t}")
        except Exception:
            pass
    conn.commit()
    conn.close()
    # 2026-08-10: raw 目录也清空（否则前序测试的 raw 文件残留 → 行级幂等误判）
    import shutil
    raw_dir = os.environ.get("MEMORY_SERVER_RAW_DIR")
    if raw_dir and os.path.isdir(raw_dir):
        for ent in os.listdir(raw_dir):
            p = os.path.join(raw_dir, ent)
            shutil.rmtree(p) if os.path.isdir(p) else os.unlink(p)
    yield
