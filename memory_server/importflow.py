#!/usr/bin/env python3
"""
memory-server 旧记忆一键导入 + 自动增强管线（F8，2026-08-11）
================================================================
开源用户下载后：`eidetic init` 向导 Step 4 或 `eidetic import-old`，
选择旧记忆路径 → 自动识别格式 → 导入记忆库 → 自动跑完整增强管线。

流程：
  1. detect_old_memory()   探测常见旧记忆目录（OpenClaw / Basic Memory / Obsidian 等）
  2. plan_import(paths)    分析路径类型，生成导入计划（zip bundle / 目录子结构 / 普通目录）
  3. run_import()          执行导入（幂等，hash 去重，不碰源文件）
  4. post_import_enhance() 自动增强：分类 → 规则实体 KG → AAAK 压缩 → 语义去重 → 向量索引
  5. 自检 + 报告

安全：
  - 源文件只读（复制进 raw/，hash 校验）
  - 所有步骤可 dry-run
  - 语义去重默认 dry-run（--apply 才真删）

用法:
  eidetic import-old --path ~/memories.zip --path ~/.openclaw/workspace/memory --apply
  eidetic import-old --detect                    # 只列出探测到的旧记忆
"""
import os
import sys
import json
import time
import argparse
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory_server import db  # noqa: E402

log = logging.getLogger("memory-server.importflow")

# 已知子目录 → namespace 映射（识别 OpenClaw/Basic Memory 目录结构）
KNOWN_SUBDIRS = {
    "dialogue": "dialogue",
    "core": "core",
    "curated": "curated",
    "learnings": "learnings",
    "facts": "facts",
    "episodic": "episodic记忆",
    "episodic记忆": "episodic记忆",
    "场景画像": "场景画像",
    "日常对话": "日常对话",
    "acme品牌": "acme品牌",
    "系统配置": "系统配置",
    "reflections": "reflections",
    "reports": "reports",
}

# 探测候选（常见旧记忆位置）
DETECT_CANDIDATES = [
    os.path.expanduser("~/.openclaw/workspace/memory/dialogue"),
    os.path.expanduser("~/.openclaw/workspace/memory/core"),
    os.path.expanduser("~/.openclaw/workspace/memory/curated"),
    os.path.expanduser("~/.openclaw/workspace/memory/facts"),
    os.path.expanduser("~/.openclaw/workspace/memory/episodic"),
    os.path.expanduser("~/.openclaw/workspace/memory/reflections"),
    os.path.expanduser("~/.openclaw/workspace/.learnings"),
    os.path.expanduser("~/.openclaw/workspace/memory"),
    os.path.expanduser("~/memories.zip"),
    os.path.expanduser("~/.basic-memory"),
    os.path.expanduser("~/Documents/Obsidian"),
]


def detect_old_memory():
    """扫描常见旧系统痕迹。返回存在的路径列表（去重，目录优先）"""
    hints = []
    seen = set()
    for p in DETECT_CANDIDATES:
        if os.path.isdir(p) or os.path.isfile(p):
            norm = os.path.normpath(p)
            if norm not in seen:
                seen.add(norm)
                hints.append(norm)
    return hints


def _has_known_subdirs(src_dir):
    """目录下是否有已知子结构（dialogue/core/...）"""
    try:
        for name in os.listdir(src_dir):
            full = os.path.join(src_dir, name)
            if os.path.isdir(full) and name in KNOWN_SUBDIRS:
                return True
    except Exception:
        pass
    return False


def plan_import(paths):
    """分析路径 → 导入计划。

    返回: [{"path": str, "kind": "bundle"|"subdirs"|"dir", "namespace": str|None,
            "sub_plans": [{"path","namespace"}], "note": str}]
    """
    plans = []
    for p in paths or []:
        p = os.path.expanduser(p.strip())
        if not os.path.exists(p):
            plans.append({"path": p, "kind": "missing", "note": "路径不存在"})
            continue
        if os.path.isfile(p):
            if p.endswith(".zip"):
                plans.append({"path": p, "kind": "bundle", "namespace": None,
                              "note": "记忆包 zip"})
            else:
                plans.append({"path": p, "kind": "file", "note": "仅支持目录或 .zip"})
            continue
        # 目录
        if _has_known_subdirs(p):
            subs = []
            for name in sorted(os.listdir(p)):
                full = os.path.join(p, name)
                if os.path.isdir(full) and name in KNOWN_SUBDIRS:
                    subs.append({"path": full, "namespace": KNOWN_SUBDIRS[name]})
            plans.append({"path": p, "kind": "subdirs", "namespace": None,
                          "sub_plans": subs,
                          "note": f"识别到 {len(subs)} 个子目录（自动映射 namespace）"})
        else:
            base = os.path.basename(os.path.normpath(p))
            ns = KNOWN_SUBDIRS.get(base, base)
            plans.append({"path": p, "kind": "dir", "namespace": ns,
                          "note": f"整目录 → namespace[{ns}]"})
    return plans


def run_import(paths, provider, dry_run=False):
    """执行导入计划。返回汇总报告 dict"""
    plans = plan_import(paths)
    report = {"plans": plans, "ok": 0, "skipped": 0, "failed": [],
              "by_namespace": {}}

    for pl in plans:
        kind = pl["kind"]
        if kind == "bundle":
            from memory_server.bundle import import_bundle
            print(f"📦 记忆包: {pl['path']}")
            r = import_bundle(pl["path"], provider, dry_run=dry_run)
            if r is None:
                report["failed"].append((pl["path"], "bundle 导入失败"))
                continue
            report["ok"] += r.get("imported", 0)
            report["skipped"] += r.get("skipped", 0)
            for ns, val in (r.get("by_namespace") or {}).items():
                # 审计修复（2026-08-11）：bundle 的 by_namespace 值是完整结果 dict（{ok,skipped,failed}）
                # 不是计数 → 取 ok 计数累加，防 int+dict TypeError
                if isinstance(val, dict):
                    n = val.get("ok", 0) or val.get("imported", 0) or 0
                else:
                    n = val or 0
                report["by_namespace"][ns] = report["by_namespace"].get(ns, 0) + n
        elif kind == "subdirs":
            from memory_server.importer import import_dir
            for sub in pl["sub_plans"]:
                print(f"  📁 {sub['path']} → namespace[{sub['namespace']}]")
                if dry_run:
                    continue
                r = import_dir(sub["path"], sub["namespace"], provider)
                report["ok"] += r["ok"]
                report["skipped"] += r["skipped"]
                report["by_namespace"][sub["namespace"]] = (
                    report["by_namespace"].get(sub["namespace"], 0) + r["ok"])
                if r["failed"]:
                    report["failed"].extend(r["failed"][:5])
        elif kind == "dir":
            from memory_server.importer import import_dir
            print(f"📁 {pl['path']} → namespace[{pl['namespace']}]")
            if dry_run:
                continue
            r = import_dir(pl["path"], pl["namespace"], provider)
            report["ok"] += r["ok"]
            report["skipped"] += r["skipped"]
            report["by_namespace"][pl["namespace"]] = (
                report["by_namespace"].get(pl["namespace"], 0) + r["ok"])
            if r["failed"]:
                report["failed"].extend(r["failed"][:5])
        elif kind == "missing":
            report["failed"].append((pl["path"], "路径不存在"))
    return report


def post_import_enhance(provider, apply_dedup=False):
    """导入后自动增强管线：分类 → 规则实体 KG → AAAK 压缩 → 语义去重 → 向量索引。

    顺序说明：
      - 分类先行（后续压缩/去重都依赖 topic 归集）
      - 语义去重最后执行（删除相似后其他步骤的产物不重复）
    默认 dedup 只报告不删除（--apply 才真删，安全第一）。
    返回步骤结果 dict
    """
    results = {}
    conn = db.get_conn()
    try:
        return _post_import_enhance_inner(conn, provider, apply_dedup, results)
    finally:
        conn.close()


def _post_import_enhance_inner(conn, provider, apply_dedup, results):
    """post_import_enhance 主体（审计🔵修复 2026-08-11 复审：conn 用后必须关闭）"""
    # 1. 自动分类（补标 topic）
    try:
        from memory_server.classifier import auto_classify
        r = auto_classify(namespace=None, limit=50000)
        results["classify"] = r
        print(f"🏷️  分类补标: 覆盖 {r.get('covered', 0)} 条"
              + (f"（{r.get('missing', 0)} 条未匹配保持原状）" if r.get("missing") else ""))
    except Exception as e:
        results["classify"] = {"error": str(e)}
        print(f"⚠️  分类补标跳过: {e}")

    # 2. 规则实体 KG 增量（确定性，零 LLM）
    try:
        from memory_server.extract import deterministic_extract
        from memory_server.kg import kg_add
        rows = conn.execute(
            "SELECT id, namespace, text FROM chunks ORDER BY id DESC LIMIT 3000"
        ).fetchall()
        added = 0
        for r in rows:
            res = deterministic_extract(r["text"])
            for f in res.get("facts", [])[:10]:
                try:
                    kg_add(f.get("subject"), f.get("predicate"), f.get("object"),
                           namespace=r["namespace"])
                    added += 1
                except Exception:
                    pass
        results["kg_rules"] = {"scanned": len(rows), "facts_added": added}
        print(f"🕸️  规则实体 KG: 扫描 {len(rows)} 条 / 新增事实 {added}")
    except Exception as e:
        results["kg_rules"] = {"error": str(e)}
        print(f"⚠️  规则实体 KG 跳过: {e}")

    # 3. AAAK 结构化压缩（规则，零 LLM）
    try:
        from memory_server.compress import compress_chunks
        r = compress_chunks(namespace=None, limit=5000, dry_run=False)
        results["aaak"] = r
        print(f"🗜️  AAAK 压缩: 生成 {r.get('generated', 0)} 条")
    except Exception as e:
        results["aaak"] = {"error": str(e)}
        print(f"⚠️  AAAK 压缩跳过: {e}")

    # 4. 语义去重（默认只报告）
    try:
        from memory_server.dedup import dedup
        r = dedup(namespace=None, threshold=0.15, dry_run=not apply_dedup, limit=50000)
        results["dedup"] = r
        print(f"🔍 语义去重: 扫描 {r['scanned']} / 相似对 {r['duplicates']} / "
              f"{'将删除' if not apply_dedup else '已删除'} {r['removed']}"
              + ("（dry-run 未删，--apply 执行）" if not apply_dedup else ""))
    except Exception as e:
        results["dedup"] = {"error": str(e)}
        print(f"⚠️  语义去重跳过: {e}")

    # 5. 向量索引重建（新导入数据进索引）
    try:
        from memory_server.vector_index import ensure_index
        r = ensure_index(conn, force=False)
        results["vector_index"] = {"status": r}
        print(f"🧮 向量索引: {r}")
    except Exception as e:
        results["vector_index"] = {"error": str(e)}
        print(f"⚠️  向量索引更新跳过: {e}")

    return results


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="eidetic import-old",
        description="旧记忆一键导入 + 自动增强管线（格式自动识别，源文件只读）")
    parser.add_argument("--path", action="append", default=[],
                        help="旧记忆路径（目录或 memories.zip），可多次指定")
    parser.add_argument("--detect", action="store_true", help="只列出探测到的旧记忆位置")
    parser.add_argument("--apply", action="store_true",
                        help="执行语义去重删除（默认只报告不删）")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写入")
    parser.add_argument("--backend", default=None,
                        choices=["llama.cpp", "ollama", "fts-only"])
    args = parser.parse_args(argv)

    db.init_db()

    if args.detect:
        hints = detect_old_memory()
        if not hints:
            print("未探测到旧记忆痕迹。")
            return 0
        print(f"🔍 探测到 {len(hints)} 处旧记忆痕迹：")
        for h in hints:
            kind = "📦" if h.endswith(".zip") else "📁"
            print(f"  {kind} {h}")
        return 0

    paths = args.path
    if not paths:
        hints = detect_old_memory()
        if hints:
            print("未指定 --path，自动使用探测到的路径：")
            for h in hints:
                print(f"  - {h}")
            paths = hints
        else:
            print("未指定路径且未探测到旧记忆。用法: eidetic import-old --path <路径>")
            return 1

    provider = None
    if not args.dry_run:
        from memory_server.embed import detect_provider
        provider = detect_provider(prefer=args.backend or None)

    print("📋 导入计划：")
    for pl in plan_import(paths):
        mark = "❌" if pl["kind"] == "missing" else "✅"
        print(f"  {mark} {pl['path']} [{pl['kind']}] {pl.get('note', '')}")

    if args.dry_run:
        print("\n🔍 dry-run：不写入。加 --apply（含 dedup 真删）执行。")
        return 0

    t0 = time.time()
    report = run_import(paths, provider, dry_run=False)
    print(f"\n📥 导入汇总: 新摄入 {report['ok']} / 跳过 {report['skipped']} / "
          f"失败 {len(report['failed'])}（{time.time()-t0:.1f}s）")
    for ns, n in report["by_namespace"].items():
        print(f"   namespace[{ns}]: +{n}")
    if report["failed"]:
        print("⚠️  失败清单（前 10）：")
        for path, err in report["failed"][:10]:
            print(f"  - {path}: {err}")

    print("\n⚙️  自动增强管线...")
    post_import_enhance(provider, apply_dedup=args.apply)

    print("\n🩺 自检...")
    try:
        from memory_server.doctor import main as doctor_main
        doctor_main(argv=[])
    except Exception as e:
        print(f"   ⚠️ 自检调用失败: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
