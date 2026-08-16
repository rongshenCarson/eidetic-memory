#!/usr/bin/env python3
"""hallway 共现轨道（对齐 MemPalace hallways.py 的 co-occurrence 走廊）

MemPalace hallway = 实体对在抽屉中共现的统计连接：
    WING → DRAWERS（各带实体）→ 实体对共现 → HALLWAY（走廊）

Eidetic 实现：扫描 chunks 文本，匹配 entities 表的实体名，
统计两两共现（同一 chunk 内出现 = 共现一次），生成走廊结构。

用法: eidetic build-hallways [--min-co 2]   # 构建走廊
      eidetic hallway <实体> --mode co      # 从走廊漫游
"""
import os
import sys
import time
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory_server import db

# 走廊文件默认存包内 data/（随 DB_DIR），
# 解除跨系统耦合；旧路径仅做只读兼容回退。
# 2026-08-12：默认路径跟随 db.DB_DIR（与日志/备份/索引统一，自定义目录部署不再分裂）
HALLWAY_FILE = os.environ.get(
    "MEMORY_HALLWAY_FILE",
    os.path.join(db.DB_DIR, "hallways.json"))  # 默认包内 data/

# 技术术语/通用词停用（非真实实体，共现走廊会淹没真实关系）
STOPWORDS = {
    "drawer", "drawers", "general", "mine", "sweep", "sync", "repair",
    "对话", "文件", "内容", "记录", "数据", "记忆", "系统", "配置",
    "workspace", "memory", "palace", "chunk", "chunks", "jsonl", "md",
    "用户", "日志", "工具", "命令", "文档", "目录", "版本", "状态",
    "brand", "topic", "room", "wing", "closet", "keyword", "verify",
    "content", "user", "assistant", "日常对话", "episodic", "系统", "acme",
    "README", "index", "main", "default", "ns", "api", "mcp", "cli",
    "openclaw", "eidetic", "sqlite", "chroma", "ollama",
    "bge", "kg", "fts", "json", "html", "py", "sh", "yml", "yaml",
}


def _is_stopword(name):
    n = name.strip().lower()
    return n in STOPWORDS or len(n) <= 1


def build_hallways(min_co=2, limit_chunks=50000):
    """扫描 chunks → 实体共现 → 走廊列表

    返回 {"hallways": [...], "stats": {...}}
    """
    conn = db.get_conn()
    try:
        # 1. 实体名（去长名 + 停用词，>30 字符的句子片段跳过）
        ents = conn.execute(
            "SELECT name FROM entities WHERE LENGTH(name) <= 30").fetchall()
        names = [r["name"].strip() for r in ents
                 if r["name"].strip() and not _is_stopword(r["name"])]
        if not names:
            return {"hallways": [], "stats": {"error": "无实体"}}

        # 2. 扫描 chunks（限文本长度，匹配实体名）
        co = {}  # (a,b) -> count
        entity_rooms = {}  # entity -> set(namespace)
        n_chunks = 0
        for row in conn.execute(
                "SELECT namespace, text FROM chunks ORDER BY id DESC LIMIT ?",
                (limit_chunks,)):
            text = row["text"]
            ns = row["namespace"]
            if not text or len(text) > 5000:
                continue
            hit = [n for n in names if n and len(n) >= 2 and n in text]
            if len(hit) < 2:
                if hit:
                    entity_rooms.setdefault(hit[0], set()).add(ns)
                continue
            n_chunks += 1
            for e in hit:
                entity_rooms.setdefault(e, set()).add(ns)
            # 两两共现
            for i in range(len(hit)):
                for j in range(i + 1, len(hit)):
                    a, b = sorted((hit[i], hit[j]))
                    if a == b:
                        continue  # 排除自对
                    key = (a, b)
                    co[key] = co.get(key, 0) + 1

        # 3. 走廊列表（过滤 min_co）
        hallways = []
        for (a, b), cnt in co.items():
            if cnt < min_co:
                continue
            rooms = entity_rooms.get(a, set()) | entity_rooms.get(b, set())
            hallways.append({
                "entity_a": a,
                "entity_b": b,
                "co_occurrence_count": cnt,
                "rooms": sorted(rooms),
                "label": f"{a} ↔ {b} (co-occur in {cnt} chunks)",
                "created_at": time.time(),
            })
        hallways.sort(key=lambda h: -h["co_occurrence_count"])
        return {"hallways": hallways,
                "stats": {"entities": len(names), "chunks_scanned": n_chunks,
                          "hallways": len(hallways)}}
    finally:
        conn.close()


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="eidetic build-hallways",
                                 description="构建实体共现走廊（对齐 MemPalace hallway）")
    ap.add_argument("--min-co", type=int, default=2, help="最小共现次数（默认 2）")
    ap.add_argument("--out", default=None, help="输出 JSON 路径（默认只打印统计）")
    args = ap.parse_args(argv)

    r = build_hallways(min_co=args.min_co)
    st = r["stats"]
    print(f"📊 实体 {st.get('entities',0)} / 扫描 {st.get('chunks_scanned',0)} chunks / "
          f"走廊 {st.get('hallways',0)} 条")
    if "error" in st:
        print(f"⚠️ {st['error']}")
        return 0
    for h in r["hallways"][:15]:
        print(f"  🧭 {h['label']} | rooms: {','.join(h['rooms'][:4])}")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=1)
        print(f"✅ 已保存: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
