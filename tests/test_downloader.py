"""模型下载器测试：断点续传/已存在跳过/损坏重下（本地 Range HTTP 服务器）"""
import sys, os, hashlib, random, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import http.server
import socketserver

from memory_server.downloader import download_model


class RangeHandler(http.server.SimpleHTTPRequestHandler):
    """支持 Range 的最小静态服务器"""
    def send_head(self):
        range_h = self.headers.get("Range")
        path = self.translate_path(self.path)
        if not os.path.isfile(path):
            return super().send_head()
        size = os.path.getsize(path)
        if range_h and range_h.startswith("bytes="):
            start, _, end = range_h[6:].partition("-")
            start = int(start) if start else 0
            end = int(end) if end else size - 1
            end = min(end, size - 1)
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(length))
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            self._range = (start, length)
            return open(path, "rb")
        return super().send_head()

    def copyfile(self, source, outputfile):
        if hasattr(self, "_range"):
            start, length = self._range
            source.seek(start)
            while length > 0:
                chunk = source.read(min(65536, length))
                if not chunk:
                    break
                outputfile.write(chunk)
                length -= len(chunk)
            del self._range
        else:
            super().copyfile(source, outputfile)

    def log_message(self, fmt, *args):
        pass


def setup_module():
    global pytest_server
    pytest_server = socketserver.TCPServer(("127.0.0.1", 0), RangeHandler)  # 动态端口
    threading.Thread(target=pytest_server.serve_forever, daemon=True).start()


def teardown_module():
    pytest_server.shutdown()


def _url():
    return f"http://127.0.0.1:{pytest_server.server_address[1]}/model.bin"


def _serve_file(tmp_path, size_mb=2):
    random.seed(42)
    data = bytes(random.getrandbits(8) for _ in range(size_mb * 1024 * 1024))
    f = tmp_path / "model.bin"
    f.write_bytes(data)
    return str(f), hashlib.sha256(data).hexdigest()


def test_resume_download(tmp_path):
    src, sha = _serve_file(tmp_path)
    os.chdir(tmp_path)  # 服务器 cwd 指向文件
    # 部分文件（模拟中断）
    with open(src, "rb") as f:
        partial = f.read(300 * 1024)
    dest = tmp_path / "partial.bin"
    dest.write_bytes(partial)

    r = download_model([_url()], str(dest),
                       expected_sha256=sha, progress=False)
    assert r["status"] == "ok" and r["resumed"] is True
    assert hashlib.sha256(dest.read_bytes()).hexdigest() == sha


def test_existing_skip(tmp_path):
    src, sha = _serve_file(tmp_path)
    os.chdir(tmp_path)
    r = download_model([_url()], src,
                       expected_sha256=sha, progress=False)
    assert r["status"] == "skipped"


def test_corrupt_redownload(tmp_path):
    src, sha = _serve_file(tmp_path)
    os.chdir(tmp_path)
    dest = tmp_path / "empty.bin"
    dest.write_bytes(b"")  # 空文件 = 损坏
    r = download_model([_url()], str(dest),
                       expected_sha256=sha, progress=False)
    assert r["status"] == "ok"
    assert hashlib.sha256(dest.read_bytes()).hexdigest() == sha


def test_existing_no_checksum_skip(tmp_path):
    """无校验值 + 已存在 → skipped（防 416）"""
    src, _ = _serve_file(tmp_path)
    os.chdir(tmp_path)
    r = download_model([_url()], src, progress=False)
    assert r["status"] == "skipped"
