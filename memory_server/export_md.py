#!/usr/bin/env python3
"""
memory-server Markdown 导出（③a，Basic Memory 借鉴）
======================================================
把记忆导出为人类可读的 Markdown（按 namespace/room 组织）——人机同读写的基础：
人类可审阅、可信任、可移植（不锁定在专有格式）。

用法:
  eidetic export-markdown [--out memory_export/] [--ns brand]
"""
import os
import sys
import json
import time
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory_server import db  # noqa: E402


def _ns_names(conn):
    rows = conn.execute("SELECT DISTINCT namespace FROM chunks").fetchall()
    # 黑名单：测试/验证 namespace 不导出（R4，2026-08-10：防测试残留泄漏进注入源）
    BLACKLIST = {"test", "verify", "demo", "e2e", "tmp"}
    return [r[0] for r in rows if r[0] not in BLACKLIST]


def export_markdown(out_dir, namespace=None):
    """导出记忆为 Markdown 文件树。

    结构: <out>/
      <namespace>/README.md      概览（统计 + 画像 + 核心记忆）
      <namespace>/extracts.md    结构化提取物（decision/fact/episodic...）
      <namespace>/kg.md          知识图谱（实体 + 三元组）
      <namespace>/scenes.md      场景归纳
      <namespace>/learnings.md   教训
      <namespace>/dialogue.md    对话原文（chunks 文本）
    """
    conn = db.get_conn()
    try:
        nss = [namespace] if namespace else _ns_names(conn)
        os.makedirs(out_dir, exist_ok=True)
        written = 0
        for ns in nss:
            ns_dir = os.path.join(out_dir, ns)
            os.makedirs(ns_dir, exist_ok=True)

            # README
            stats = conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE namespace=?", (ns,)).fetchone()[0]
            # 审计 P0-3（2026-08-11 三审）：persona/curated 存 'default'、scenes 存 ''，
            # 原按 ns 精确过滤 → 永不导出（Active Memory 注入源只有统计+对话摘录）。
            # 改为全库兜底：优先本 ns，无则全库最新（衍生层是全局资产，不分业务 ns）
            persona = conn.execute(
                "SELECT content FROM persona ORDER BY updated_at DESC LIMIT 1").fetchone()
            curated = conn.execute(
                "SELECT content FROM curated ORDER BY updated_at DESC LIMIT 1").fetchone()
            lines = [f"# {ns} 记忆概览", "",
                     f"- chunks: {stats}", f"- 导出时间: {time.strftime('%Y-%m-%d %H:%M')}", ""]
            if persona:
                lines += ["## 身份画像", "", persona[0], ""]
            if curated:
                lines += ["## 核心归集", "", curated[0], ""]
            _write(os.path.join(ns_dir, "README.md"), "\n".join(lines))
            written += 1

            # extracts
            rows = conn.execute(
                "SELECT type, text, short, importance, timestamp FROM extracts "
                "WHERE namespace=? ORDER BY importance DESC, id DESC", (ns,)).fetchall()
            if rows:
                lines = ["# 结构化提取物", ""]
                for r in rows:
                    lines.append(f"## [{r['type']}] ★{r['importance']} ({r['timestamp'] or '?'})")
                    lines.append(r["text"])
                    if r["short"] and r["short"] != r["text"]:
                        lines.append(f"\n> 短版: {r['short']}")
                    lines.append("")
                _write(os.path.join(ns_dir, "extracts.md"), "\n".join(lines))
                written += 1

            # kg
            ents = conn.execute(
                "SELECT name, canonical FROM entities WHERE namespace=?", (ns,)).fetchall()
            trips = conn.execute(
                "SELECT subject, predicate, object, valid_from, valid_to FROM triples "
                "WHERE namespace=?", (ns,)).fetchall()
            if ents or trips:
                lines = ["# 知识图谱", ""]
                if ents:
                    lines += ["## 实体", ""]
                    for e in ents:
                        mark = "" if e["name"] == e["canonical"] else f" → {e['canonical']}"
                        lines.append(f"- {e['name']}{mark}")
                    lines.append("")
                if trips:
                    lines += ["## 三元组", ""]
                    for t in trips:
                        vt = f" ~ {t['valid_to']}" if t["valid_to"] else ""
                        lines.append(f"- {t['subject']} {t['predicate']} {t['object']} "
                                     f"({t['valid_from'] or '?'}{vt})")
                    lines.append("")
                _write(os.path.join(ns_dir, "kg.md"), "\n".join(lines))
                written += 1

            # scenes（审计 P0-3：scenes 存 ''，全库读取）
            scenes = conn.execute(
                "SELECT date, title, content FROM scenes ORDER BY date DESC").fetchall()
            if scenes:
                lines = ["# 场景归纳", ""]
                for s in scenes:
                    lines.append(f"## {s['date']} {s['title'] or ''}")
                    lines.append(s["content"])
                    lines.append("")
                _write(os.path.join(ns_dir, "scenes.md"), "\n".join(lines))
                written += 1

            # learnings（审计 P0-3：learnings 存 default/业务 ns，全库读取）
            lrns = conn.execute(
                "SELECT code, title, lesson FROM learnings ORDER BY id DESC").fetchall()
            if lrns:
                lines = ["# 教训", ""]
                for l in lrns:
                    lines.append(f"## [{l['code']}] {l['title']}")
                    if l["lesson"]:
                        lines.append(l["lesson"])
                    lines.append("")
                _write(os.path.join(ns_dir, "learnings.md"), "\n".join(lines))
                written += 1

            # dialogue（前 200 条原文）
            dlg = conn.execute(
                "SELECT text, updated_at FROM chunks WHERE namespace=? "
                "ORDER BY id DESC LIMIT 200", (ns,)).fetchall()
            if dlg:
                lines = ["# 对话原文（最近 200 条）", ""]
                for d in reversed(dlg):
                    ts = time.strftime("%m-%d %H:%M", time.localtime(d["updated_at"]))
                    lines.append(f"- [{ts}] {d['text'][:200]}")
                _write(os.path.join(ns_dir, "dialogue.md"), "\n".join(lines))
                written += 1
        return {"namespaces": len(nss), "files": written, "out": out_dir}
    finally:
        conn.close()


def _write(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="eidetic export-markdown", description="导出人类可读 Markdown")
    parser.add_argument("--out", default="memory_export/", help="输出目录")
    parser.add_argument("--ns", default=None)
    args = parser.parse_args(argv)
    db.init_db()
    r = export_markdown(args.out, namespace=args.ns)
    print(f"📤 已导出 {r['namespaces']} 个 namespace / {r['files']} 个文件 → {r['out']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
