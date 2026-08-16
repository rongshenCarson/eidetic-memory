#!/usr/bin/env python3
"""
memory-server 存储后端抽象（⑤，A+B 可插拔）
==============================================
双后端：
  - sqlite（默认）：自建单库（现状）
  - mempalace：封装 MemPalace 3.5.0（subprocess 调 CLI，不污染 Eidetic venv）

切换：config.yaml 的 storage.backend 字段，或命令 --backend 参数。
验证：eidetic backend-compare（同一查询双后端比对，复用影子期思路）。

用法:
  eidetic search "词" --backend mempalace
  eidetic backend-compare "词" "词2" ...
"""
import os
import sys
import json
import shutil
import subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MEMPALACE_BIN = os.path.expanduser("~/.mempalace-venv/bin/mempalace")
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".memory-server", "config.yaml")


class StorageBackend:
    name = "base"

    def search(self, query, namespace=None, limit=5, **kw):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError

    def health(self):
        return True


class SqliteBackend(StorageBackend):
    """自建单库（默认后端）"""
    name = "sqlite"

    def search(self, query, namespace=None, limit=5, fusion=False, **kw):
        from memory_server.search import search as s_search
        from memory_server.search import fusion_search
        if fusion:
            r = fusion_search(query, namespace=namespace, limit=limit,
                              provider=kw.get("provider"), embed=kw.get("embed", True),
                              task_context=kw.get("task_context"), room=kw.get("room"),
                              depth=kw.get("depth", 1))
            return r["results"]
        return s_search(query, namespace=namespace, limit=limit,
                        provider=kw.get("provider"), embed=kw.get("embed", True),
                        room=kw.get("room"))

    def stats(self):
        from memory_server.search import stats
        return stats()

    def health(self):
        try:
            self.stats()
            return True
        except Exception:
            return False


# 审计三审 P2-11（2026-08-11）：MemPalace 已停用（2026-08-11 全部禁用），
# 此适配器为 legacy 遗留，仅 backend-compare 对比工具引用，不做功能维护。
class MemPalaceBackend(StorageBackend):  # LEGACY：旧系统适配器，勿用于生产
    """MemPalace 3.5.0 后端（subprocess CLI 适配）"""
    name = "mempalace"

    def __init__(self, bin_path=None):
        self.bin = bin_path or MEMPALACE_BIN

    def _run(self, args):
        if not os.path.exists(self.bin):
            return {"error": f"mempalace CLI 不存在: {self.bin}"}
        r = subprocess.run([self.bin] + args, capture_output=True, text=True, timeout=60)
        return {"rc": r.returncode, "stdout": r.stdout, "stderr": r.stderr}

    def search(self, query, namespace=None, limit=5, **kw):
        """调 mempalace search。namespace 映射为 wing（MemPalace 主题翼）"""
        args = ["search", query, "--results", str(limit)]
        if namespace:
            args += ["--wing", namespace]
        r = self._run(args)
        if "error" in r:
            return [{"score": 0, "text": f"⚠️ {r['error']}"}]
        if r.get("rc") != 0:
            return []
        # 解析 CLI 输出（Results for: "..." 后跟 [n] 行）
        results = []
        in_results = False
        for line in r["stdout"].splitlines():
            line = line.strip()
            if line.startswith("Results for"):
                in_results = True
                continue
            if in_results and line.startswith("["):
                # 提取内容（去掉行号前缀，取后两行）
                continue
            if in_results and line and not line.startswith("=") and not line.startswith("Match"):
                if line.startswith("Source") or line.startswith("Wing") or line.startswith("Room"):
                    continue
                if line.startswith("Match"):
                    import re
                    m = re.search(r"cosine_sim=([\d.]+)", line)
                    if m and results:
                        results[-1]["score"] = float(m.group(1))
                    continue
                results.append({"score": 0.0, "text": line[:200], "backend": "mempalace"})
        return results

    def stats(self):
        r = self._run(["status"])
        if "error" in r:
            return {"error": r["error"]}
        return {"raw": r["stdout"][:500]}

    def health(self):
        r = self._run(["status"])
        return "error" not in r and r.get("rc", 1) == 0


def get_backend(name=None, config_path=None):
    """后端工厂：config.storage.backend 或显式参数"""
    if name is None:
        name = "sqlite"
        cfg = config_path or CONFIG_PATH
        if os.path.exists(cfg):
            try:
                for line in open(cfg):
                    if line.strip().startswith("backend:"):
                        name = line.split(":", 1)[1].strip()
                        break
            except Exception:
                pass
    if name == "mempalace":
        return MemPalaceBackend()
    return SqliteBackend()


def backend_compare(queries, limit=5, namespace=None):
    """双后端比对（复用影子期思路）：同一查询跑 sqlite + mempalace"""
    sql = SqliteBackend()
    mp = MemPalaceBackend()
    rows = []
    for q in queries:
        s_res = sql.search(q, namespace=namespace, limit=limit)
        m_res = mp.search(q, namespace=namespace, limit=limit)
        s_texts = [r["text"][:60] for r in s_res]
        m_texts = [r["text"][:60] for r in m_res]
        overlap = len(set(s_texts) & set(m_texts))
        rows.append({"query": q, "sqlite_top1": s_texts[0] if s_texts else "-",
                     "mempalace_top1": m_texts[0] if m_texts else "-",
                     "overlap": overlap})
    return rows


def main_compare(argv=None):
    import argparse
    parser = argparse.ArgumentParser(prog="eidetic backend-compare",
                                     description="双后端检索比对（sqlite vs mempalace）")
    parser.add_argument("queries", nargs="+", help="查询词（可多个）")
    parser.add_argument("--ns", default=None)
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args(argv)

    from memory_server import db
    db.init_db()
    rows = backend_compare(args.queries, limit=args.limit, namespace=args.ns)
    for r in rows:
        print(f"「{r['query']}」 重叠 {r['overlap']}/{args.limit}")
        print(f"  sqlite  : {r['sqlite_top1']}")
        print(f"  mempalace: {r['mempalace_top1']}")
    return 0
