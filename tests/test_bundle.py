"""记忆包测试：导出/导入闭环/幂等/损坏拦截"""
import sys, os, zipfile, hashlib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory_server import db
from memory_server.bundle import export_bundle, import_bundle
from memory_server.embed import FtsOnlyProvider
from memory_server.search import search


def _make_corpus(tmp_path):
    (tmp_path / "old" / "dialogue").mkdir(parents=True)
    (tmp_path / "old" / "core").mkdir(parents=True)
    (tmp_path / "old" / "dialogue" / "2026-08-01.md").write_text(
        "# 对话\n今天讨论了记忆系统迁移方案\n", encoding="utf-8")
    (tmp_path / "old" / "core" / "note.md").write_text(
        "# 核心记忆\ntest 记忆包导出\n", encoding="utf-8")
    return str(tmp_path / "old")


def test_export_import_roundtrip(tmp_path):
    db.init_db()
    src = _make_corpus(tmp_path)
    out_zip = str(tmp_path / "memories.zip")
    result = export_bundle([src + "/dialogue", src + "/core"], out_zip)
    assert result and result["file_count"] == 2

    # 校验 zip 结构
    with zipfile.ZipFile(out_zip) as zf:
        names = zf.namelist()
        assert "manifest.json" in names and "CHECKSUMS.txt" in names
        assert any(n.startswith("raw/") for n in names)

    # 导入
    r = import_bundle(out_zip, FtsOnlyProvider())
    assert r and r["imported"] == 2 and not r["failed"]

    # 检索闭环
    hits = search("记忆系统迁移", namespace="dialogue", limit=3,
                  provider=FtsOnlyProvider(), embed=False)
    assert hits and "迁移方案" in hits[0]["text"]


def test_reimport_idempotent(tmp_path):
    db.init_db()
    src = _make_corpus(tmp_path)
    out_zip = str(tmp_path / "memories.zip")
    export_bundle([src + "/dialogue"], out_zip)
    import_bundle(out_zip, FtsOnlyProvider())
    r2 = import_bundle(out_zip, FtsOnlyProvider())
    assert r2 and r2["imported"] == 0 and r2["skipped"] == 1  # 不翻倍


def test_corrupt_detection(tmp_path):
    db.init_db()
    src = _make_corpus(tmp_path)
    out_zip = str(tmp_path / "memories.zip")
    export_bundle([src + "/dialogue"], out_zip)

    # 篡改一个文件
    corrupt = str(tmp_path / "corrupt.zip")
    with zipfile.ZipFile(out_zip) as zin, zipfile.ZipFile(corrupt, "w") as zout:
        for item in zin.namelist():
            data = zin.read(item)
            if item.endswith(".md"):
                data = data.replace("迁移".encode(), "损坏".encode())
            zout.writestr(item, data)

    r = import_bundle(corrupt, FtsOnlyProvider())
    assert r and len(r["corrupt"]) == 1  # 损坏被拦截
    # 损坏文件不落库
    conn = db.get_conn()
    n = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    conn.close()
    assert n == 0
