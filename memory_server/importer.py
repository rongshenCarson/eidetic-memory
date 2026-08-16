#!/usr/bin/env python3
"""
memory-server 迁移导入器（P1b）
================================
职责：从旧系统原始层导入数据到新系统（只读复制 + hash 校验 + 幂等）
对应迁移方案 P0.5 Phase 1：
  memory-server import --source <dir> --ns <namespace>

旧系统原始层目录映射（迁移方案定稿）：
  memory/dialogue/   → ns=dialogue   （对话原文 164 文件）
  memory/core/       → ns=core       （核心提炼 447 文件）
  .learnings/        → ns=learnings  （教训 6 文件）
  memory/curated/    → ns=curated    （高频知识）
  memory/projects/   → ns=projects
  memory/facts/      → ns=facts
  memory/wisdom/     → ns=wisdom

原则（架构原则一「原始层为源」）：
  - 只读复制：不修改/移动原文件
  - 复制后 hash 校验：源与目标 sha256 一致才入库
  - 幂等：已摄入的 hash 自动跳过（补跑不翻倍）
  - 失败续跑：单文件失败不影响其他文件
"""
import os
import sys
import json
import time
import hashlib
import argparse

# 允许导入到 memory-server（避免项目根目录被整个扫入）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory_server import db  # noqa: E402
from memory_server.ingest import ingest_file, ingest_dir, RAW_DIR  # noqa: E402

# 旧系统原始层 → 推荐 namespace 映射
DEFAULT_MAPPING = {
    "dialogue": "dialogue",
    "core": "core",
    "learnings": "learnings",
    "curated": "curated",
    "projects": "projects",
    "facts": "facts",
    "wisdom": "wisdom",
    "reflections": "reflections",
    "user": "user",
    "reports": "reports",
}


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def import_dir(src_dir, namespace, provider, dry_run=False, pattern=(".md", ".jsonl")):
    """导入一个目录：只读复制到 raw/ + hash 校验 + 摄入（幂等）

    返回: {"ok": n, "skipped": n, "failed": [(path, err)], "raw_copied": n}
    """
    if not os.path.isdir(src_dir):
        return {"ok": 0, "skipped": 0, "failed": [(src_dir, "源目录不存在")], "raw_copied": 0}

    results = {"ok": 0, "skipped": 0, "failed": [], "raw_copied": 0}
    conn = db.get_conn()
    try:
        raw_ns_dir = os.path.join(RAW_DIR, namespace)
        os.makedirs(raw_ns_dir, exist_ok=True)

        files = []
        for root, _, fns in os.walk(src_dir):
            for fn in sorted(fns):
                if fn.endswith(pattern):
                    files.append(os.path.join(root, fn))
        files.sort()

        for path in files:
            try:
                h = sha256_file(path)
                # 幂等检查（基于内容 hash）
                dup = conn.execute(
                    "SELECT 1 FROM ingested_hashes WHERE hash=?", (h,)
                ).fetchone()
                if dup:
                    results["skipped"] += 1
                    continue

                # 只读复制到 raw/（带 hash 前缀，可追溯来源）
                rel = os.path.relpath(path, src_dir).replace("/", "__")
                raw_path = os.path.join(raw_ns_dir, f"{h[:8]}-{rel}")
                if not os.path.exists(raw_path):
                    with open(path, "rb") as src, open(raw_path, "wb") as dst:
                        dst.write(src.read())
                # 复制后 hash 校验（迁移方案 Phase 1 要求）
                if sha256_file(raw_path) != h:
                    results["failed"].append((path, "复制后 hash 校验失败"))
                    continue
                results["raw_copied"] += 1

                if dry_run:
                    results["ok"] += 1
                    continue

                # 幂等清理：清理上次嵌入失败留下的半截 sources 记录（sources 在嵌入前插入，
                # 嵌入失败会留下孤儿记录 → 重跑撞 UNIQUE 约束）
                conn.execute("DELETE FROM sources WHERE namespace=? AND path=?",
                             (namespace, raw_path))
                conn.commit()
                # 摄入（文件已在 raw/，skip_copy=True 避免二次复制；ingest_file 会再次 hash 去重，双保险）
                r = ingest_file(raw_path, namespace, provider, skip_copy=True)
                if r["status"] == "ok":
                    results["ok"] += 1
                elif r["status"] == "skip":
                    results["skipped"] += 1
                else:
                    results["failed"].append((path, str(r)))
            except Exception as e:
                results["failed"].append((path, str(e)))
    finally:
        conn.close()
    return results


def main():
    parser = argparse.ArgumentParser(prog="eidetic import", description="迁移导入器（旧系统原始层 → 新系统）")
    parser.add_argument("--source", required=True, help="源目录（如 memory/dialogue/）")
    parser.add_argument("--ns", default=None, help="目标 namespace（默认按目录名自动映射）")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写入")
    parser.add_argument("--fts-only", action="store_true", help="仅关键词模式（不加载嵌入模型）")
    args = parser.parse_args()

    db.init_db()

    # namespace 自动映射：--source 的目录名 → DEFAULT_MAPPING
    src_base = os.path.basename(os.path.normpath(args.source))
    namespace = args.ns or DEFAULT_MAPPING.get(src_base, src_base)

    if args.dry_run:
        provider = None
    else:
        from memory_server.embed import detect_provider
        provider = detect_provider(prefer="fts-only" if args.fts_only else None)

    print(f"📥 导入: {args.source} → namespace[{namespace}]"
          f"{' (dry-run)' if args.dry_run else ''}")
    results = import_dir(args.source, namespace, provider, dry_run=args.dry_run)

    print(f"\n✅ 导入完成: 新摄入 {results['ok']} / 已存在跳过 {results['skipped']} / 失败 {len(results['failed'])}")
    print(f"📄 原始层复制: {results['raw_copied']} 文件（hash 校验通过）")
    if results["failed"]:
        print("\n❌ 失败清单（前 10 条）:")
        for path, err in results["failed"][:10]:
            print(f"  {path}: {err}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
