"""安装向导落地逻辑测试（apply_install：写配置/初始化/导入/自检）"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import memory_server.installer as inst


def test_apply_install_writes_config(tmp_path, monkeypatch):
    # 隔离配置路径
    monkeypatch.setattr(inst, "CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(inst, "CONFIG_PATH", str(tmp_path / "cfg" / "config.yaml"))
    # 用 fts-only 避免加载模型
    config = {
        "language": "zh",
        "embedding": {"provider": "fts-only", "model": "bge-m3", "dim": 1024},
        "fts": {"tokenizer": "trigram"},
        "server": {"host": "127.0.0.1", "port": 8765},
    }
    rc = inst.apply_install(config)
    assert rc == 0  # 自检通过（fts-only 无模型依赖）
    assert os.path.exists(inst.CONFIG_PATH)
    content = open(inst.CONFIG_PATH).read()
    assert "language: zh" in content and "provider: fts-only" in content


def test_apply_install_with_bundle_import(tmp_path, monkeypatch):
    monkeypatch.setattr(inst, "CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(inst, "CONFIG_PATH", str(tmp_path / "cfg" / "config.yaml"))
    # 造一个记忆包
    from memory_server.bundle import export_bundle
    (tmp_path / "old" / "dialogue").mkdir(parents=True)
    (tmp_path / "old" / "dialogue" / "a.md").write_text("# t\n导入测试内容\n", encoding="utf-8")
    out_zip = str(tmp_path / "mem.zip")
    export_bundle([str(tmp_path / "old" / "dialogue")], out_zip)

    config = {"language": "zh",
              "embedding": {"provider": "fts-only", "model": "bge-m3", "dim": 1024},
              "fts": {"tokenizer": "trigram"},
              "server": {"host": "127.0.0.1", "port": 8765}}
    rc = inst.apply_install(config, import_paths=[out_zip])
    assert rc == 0
    # 导入的数据在库里
    from memory_server import db
    conn = db.get_conn()
    n = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    conn.close()
    assert n >= 1
