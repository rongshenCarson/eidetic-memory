"""四审补测（2026-08-11）：scheduler 退避 / backup 往返 / http_api 契约 / zip slip"""
import sys, os, time, json, shutil, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


# ---- 1. scheduler 退避/补跑/水位线 ----
class TestScheduler:
    def test_failure_backoff(self, tmp_path):
        from memory_server import db as _db
        _db.DB_DIR = str(tmp_path / "db")
        _db.DB_PATH = os.path.join(_db.DB_DIR, "memory.db")
        _db.init_db()
        from memory_server.scheduler import Task

        fails = {"n": 0}
        def flaky():
            fails["n"] += 1
            raise RuntimeError("boom")
        t = Task("test_backoff", 3600, flaky)
        t.run()  # 失败
        assert t._consecutive_failures == 1
        assert _db.get_watermark("test_backoff") is None  # 失败不写水位线
        t.stop()

    def test_success_resets_backoff(self, tmp_path):
        from memory_server import db as _db
        _db.DB_DIR = str(tmp_path / "db2")
        _db.DB_PATH = os.path.join(_db.DB_DIR, "memory.db")
        _db.init_db()
        from memory_server.scheduler import Task

        def ok():
            return None
        t = Task("test_ok", 3600, ok)
        t._consecutive_failures = 3
        t.run()
        assert t._consecutive_failures == 0
        assert _db.get_watermark("test_ok") is not None
        t.stop()


# ---- 2. backup 往返（含 WAL 热备） ----
class TestBackupRoundTrip:
    def test_backup_restore_roundtrip(self, tmp_path):
        from memory_server import db as _db
        _db.DB_DIR = str(tmp_path / "db")
        _db.DB_PATH = os.path.join(_db.DB_DIR, "memory.db")
        _db.init_db()
        from memory_server.backup import backup, restore, BACKUP_DIR

        # 写一条数据
        conn = _db.get_conn()
        conn.execute("INSERT INTO sources(namespace, path, hash, mtime, size) VALUES('test','/x','h',1,1)")
        sid = conn.execute("SELECT id FROM sources WHERE path='/x'").fetchone()[0]
        conn.execute(
            "INSERT INTO chunks(source_id, namespace, path, start_line, end_line, text, embedding, updated_at, metadata) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (sid, 'test', '/x', 0, 0, '备份往返测试内容', None, time.time(), '{}'))
        conn.commit()
        conn.close()

        # 备份
        r = backup(keep=3, out_dir=str(tmp_path / "backups"))
        assert r.get("path") and os.path.exists(r["path"]), f"备份失败: {r}"

        # 删库模拟损坏
        _db.get_conn().close()
        for p in (_db.DB_PATH, _db.DB_PATH + "-wal", _db.DB_PATH + "-shm"):
            if os.path.exists(p):
                os.remove(p)

        # 恢复
        rr = restore(r["path"], out_dir=str(tmp_path / "backups"))
        assert rr.get("restored"), f"恢复失败: {rr}"

        # 验证数据在
        conn = _db.get_conn()
        n = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        assert n >= 1, "恢复后 chunks 为空"
        conn.close()


# ---- 3. http_api 契约 + Host 校验 ----
class TestHttpApi:
    def test_host_check_and_health(self, tmp_path):
        from memory_server import db as _db
        _db.DB_DIR = str(tmp_path / "db")
        _db.DB_PATH = os.path.join(_db.DB_DIR, "memory.db")
        _db.init_db()
        import threading, urllib.request, urllib.error
        from http.server import ThreadingHTTPServer
        import memory_server.http_api as H
        from memory_server.embed import FtsOnlyProvider

        H.Handler.provider = FtsOnlyProvider()
        test_srv = ThreadingHTTPServer(("127.0.0.1", 0), H.Handler)
        t = threading.Thread(target=test_srv.serve_forever, daemon=True)
        t.start()
        port = test_srv.server_address[1]

        try:
            # health 正常
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as resp:
                assert resp.status == 200
                assert json.loads(resp.read())["status"] == "ok"
            # 恶意 Host 头 → 403（用 http.client 精确控制 Host，urllib 会强制覆盖）
            import http.client
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("GET", "/health", headers={"Host": "evil.com"})
            r = c.getresponse()
            assert r.status == 403, f"恶意 Host 应 403, got {r.status}"
            r.read()
            c.close()
        finally:
            test_srv.shutdown()
            test_srv.server_close()


# ---- 4. zip slip 防护 ----
class TestZipSlip:
    def test_zip_slip_rejected(self, tmp_path):
        import zipfile
        from memory_server.bundle import _verify_checksums, CHECKSUMS_NAME

        # 造一个带 CHECKSUMS.txt 的恶意 zip：条目含 ../
        evil = str(tmp_path / "evil.zip")
        with zipfile.ZipFile(evil, "w") as zf:
            zf.writestr(CHECKSUMS_NAME, "dummy  checksum 0\n")
            zf.writestr("raw/../../evil.txt", "pwned")

        tmpdir = str(tmp_path / "out")
        os.makedirs(tmpdir)
        result = _verify_checksums(evil, tmpdir)
        assert result is not None, "校验函数返回 None"
        # 路径穿越条目必须进 corrupt 且文件未写出
        assert not os.path.exists(os.path.join(tmp_path, "evil.txt")), \
            "zip slip 写出了库外文件"
        assert any(".." in str(x[0]) or "穿越" in str(x[1]) or "zip slip" in str(x[1]).lower()
                   for x in result.get("corrupt", [])), f"未被标记: {result}"
