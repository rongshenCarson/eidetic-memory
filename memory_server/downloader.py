#!/usr/bin/env python3
"""
memory-server 模型下载器（P1 收尾 2026-08-09）
================================================
GGUF 模型下载：断点续传（HTTP Range）+ 进度显示 + hash/大小校验。

用法（installer 调用）：
  from memory_server.downloader import download_model
  download_model(url, dest, expected_sha256=None, progress=True)

镜像策略（方案书 v0.3 §10.5）：
  modelscope 优先（国内可达），huggingface 备选；自动选可达源。
"""
import os
import sys
import time
import hashlib
import urllib.request

CHUNK = 256 * 1024  # 256KB


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _pick_source(urls, timeout=4):
    """选可达源：按顺序探测 HEAD（或 GET 小范围）"""
    for url in urls:
        try:
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                if r.status < 400:
                    return url
        except Exception:
            continue
    return urls[0] if urls else None


def download_model(urls, dest, expected_sha256=None, expected_size=None, progress=True):
    """断点续传下载。urls = [候选 URL 列表]，自动选可达源。

    返回: {"status": "ok"|"skipped"|"error", "path": dest, "size": n,
           "resumed": bool, "sha256": str, "msg": str}
    """
    if not isinstance(urls, (list, tuple)):
        urls = [urls]

    # 已存在完整文件 → 校验或跳过
    if os.path.exists(dest):
        size = os.path.getsize(dest)
        # 未指定校验值 → 视为已下载，直接跳过（避免 Range 越界 416）
        if not expected_size and not expected_sha256:
            return {"status": "skipped", "path": dest, "size": size,
                    "msg": "已存在"}
        size_ok = (not expected_size) or (size == expected_size)
        if size_ok:
            if expected_sha256:
                if sha256_file(dest) == expected_sha256:
                    return {"status": "skipped", "path": dest, "size": size,
                            "sha256": expected_sha256, "msg": "已存在且校验通过"}
            else:
                return {"status": "skipped", "path": dest, "size": size,
                        "msg": "已存在（大小匹配）"}
        # 大小/校验不匹配 → 重新下载（可能是损坏/旧版本）

    url = _pick_source(urls)
    if not url:
        return {"status": "error", "path": dest, "msg": "所有镜像源均不可达"}

    # 断点续传：已有部分文件 → Range 续传
    existing = os.path.getsize(dest) if os.path.exists(dest) else 0
    resumed = existing > 0
    headers = {"Range": f"bytes={existing}-"} if resumed else {}
    req = urllib.request.Request(url, headers=headers)

    t0 = time.time()
    last_report = 0
    total = existing
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            # 服务器支持 Range 时 status=206；不支持则返回 200 全文（从头下）
            if resp.status == 200 and resumed:
                # 服务器忽略 Range → 从头写
                mode = "wb"
                total = 0
                resumed = False
            else:
                mode = "ab" if resumed else "wb"
            content_length = resp.headers.get("Content-Length")
            total = existing if resumed else 0
            if content_length:
                total += int(content_length)
            elif resp.status == 206:
                # Range 响应：Content-Range: bytes start-end/total
                cr = resp.headers.get("Content-Range", "")
                if "/" in cr:
                    total = int(cr.split("/")[-1])

            os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)
            with open(dest, mode) as f:
                downloaded = existing if (resumed and mode == "ab") else 0
                while True:
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress and time.time() - last_report > 0.5:
                        last_report = time.time()
                        pct = downloaded / total * 100 if total else 0
                        print(f"\r   ⏬ 下载中: {downloaded/1024/1024:.1f}/{total/1024/1024:.1f} MB "
                              f"({pct:.0f}%) {time.time()-t0:.0f}s", end="", flush=True)
        if progress:
            print()
    except Exception as e:
        # 中断：保留部分文件，下次续传
        return {"status": "error", "path": dest, "size": os.path.getsize(dest) if os.path.exists(dest) else 0,
                "resumed": resumed, "msg": f"下载中断（已保留 {existing/1024/1024:.1f}MB，可续传）: {e}"}

    size = os.path.getsize(dest)
    # 校验
    if expected_size and size != expected_size:
        return {"status": "error", "path": dest, "size": size,
                "msg": f"大小不匹配: 期望 {expected_size} 实际 {size}"}
    sha = sha256_file(dest) if expected_sha256 else None
    if expected_sha256 and sha != expected_sha256:
        return {"status": "error", "path": dest, "size": size, "sha256": sha,
                "msg": f"sha256 不匹配: 期望 {expected_sha256[:16]}... 实际 {sha[:16]}..."}
    return {"status": "ok", "path": dest, "size": size, "resumed": resumed,
            "sha256": sha, "msg": f"下载完成 ({size/1024/1024:.0f}MB)"}


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(prog="eidetic download-model", description="下载嵌入模型（断点续传）")
    parser.add_argument("--dest", default=None, help="目标路径（默认 models/bge-m3-Q8_0.gguf）")
    args = parser.parse_args(argv)
    from memory_server.installer import MODEL_DIR, DEFAULT_GGUF, GGUF_DOWNLOAD_HINT
    dest = args.dest or os.path.join(MODEL_DIR, DEFAULT_GGUF)
    urls = [GGUF_DOWNLOAD_HINT["modelscope"], GGUF_DOWNLOAD_HINT["huggingface"]]
    print(f"📥 下载模型 → {dest}")
    r = download_model(urls, dest, progress=True)
    print(f"{'✅' if r['status'] in ('ok', 'skipped') else '⚠️'} {r['msg']}")
    return 0 if r["status"] in ("ok", "skipped") else 1


if __name__ == "__main__":
    sys.exit(main())
