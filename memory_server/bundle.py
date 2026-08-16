#!/usr/bin/env python3
"""
memory-server 记忆包（Memory Bundle）模块（方案书 v0.3 §10.3）
================================================================
记忆包 = 旧系统原始层的标准迁移载体：导出先行、hash 可校验、导入审计。

结构（zip）：
  memories.zip
  ├── manifest.json          # 格式版本/导出时间/来源系统/统计
  ├── meta/export_info.json  # 导出工具版本/语言分布/完整度报告
  ├── raw/<namespace>/...    # 原始文件（按 namespace 分组，保留相对路径）
  └── CHECKSUMS.txt          # 每文件 sha256 + 路径 + 大小

命令（挂载于 CLI）：
  memory-server export --source <dir> [--source <dir>...] --out memories.zip
  memory-server import --bundle memories.zip [--dry-run] [--backend ...]

原则（方案书 v0.3 §10.1）：
  - 完整性三重可核：导出清单（manifest）→ 传输校验（CHECKSUMS）→ 导入审计（报告）
  - 导出只读：不修改源文件
  - 导入幂等：复用 ingested_hashes 去重，补跑不翻倍
"""
import os
import sys
import json
import time
import shutil
import zipfile
import tempfile
import hashlib
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory_server.importer import import_dir, sha256_file, DEFAULT_MAPPING  # noqa: E402

BUNDLE_FORMAT_VERSION = "1.0"
RAW_PREFIX = "raw"
META_DIR = "meta"
CHECKSUMS_NAME = "CHECKSUMS.txt"
MANIFEST_NAME = "manifest.json"
EXPORT_INFO_NAME = "export_info.json"

# 导出默认包含的文件后缀
EXPORT_PATTERNS = (".md", ".jsonl", ".json", ".txt")


# ---------------------------------------------------------------- 语言分布（信息性统计）

def detect_language_profile(paths):
    """简单启发式语言分布统计：CJK / 拉丁 / 其他 字符占比。

    注意：这是**信息性统计**（写入 export_info.json 供用户参考），
    不替代安装向导的语言询问——用户习惯语言是意图，只能问（方案书 v0.3 §10.5）。
    """
    cjk = latin = other = total = 0
    sampled = 0
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read(20000)
        except Exception:
            continue
        sampled += 1
        for ch in text:
            cp = ord(ch)
            if 0x4E00 <= cp <= 0x9FFF or 0x3040 <= cp <= 0x30FF or 0xAC00 <= cp <= 0xD7AF:
                cjk += 1
            elif ch.isalpha():
                latin += 1
            else:
                continue
            total += 1
    if total == 0:
        return {"sampled_files": sampled, "cjk_ratio": None, "latin_ratio": None,
                "note": "无文本样本，语言分布未知"}
    return {
        "sampled_files": sampled,
        "cjk_ratio": round(cjk / total, 3),
        "latin_ratio": round(latin / total, 3),
        "note": "信息性统计，不替代安装向导的语言询问（用户习惯语言 = 意图，只能问）",
    }


# ---------------------------------------------------------------- 导出

def collect_sources(src_dirs):
    """收集源目录下的可导出文件，返回 [(abs_path, rel_path_in_zip), ...]"""
    files = []
    for src in src_dirs:
        if not os.path.isdir(src):
            print(f"  ⚠️ 源目录不存在，跳过: {src}")
            continue
        base = os.path.basename(os.path.normpath(src))
        for root, _, fns in os.walk(src):
            for fn in sorted(fns):
                if fn.endswith(EXPORT_PATTERNS):
                    abs_path = os.path.join(root, fn)
                    rel = os.path.relpath(abs_path, src)
                    zip_rel = os.path.join(RAW_PREFIX, base, rel)
                    files.append((abs_path, zip_rel))
    return files


def export_bundle(src_dirs, out_path):
    """导出记忆包：扫描源目录 → 打包 zip（manifest + CHECKSUMS + export_info + raw/）

    返回: {"file_count": n, "bytes": n, "namespaces": {...}, "out": out_path}
    """
    t0 = time.time()
    files = collect_sources(src_dirs)
    if not files:
        print("❌ 没有找到可导出的文件（支持 .md/.jsonl/.json/.txt）")
        return None

    # 统计
    total_bytes = sum(os.path.getsize(p) for p, _ in files)
    ns_stats = {}
    for p, zip_rel in files:
        ns = zip_rel.split("/")[1]
        s = ns_stats.setdefault(ns, {"file_count": 0, "bytes": 0})
        s["file_count"] += 1
        s["bytes"] += os.path.getsize(p)

    lang = detect_language_profile([p for p, _ in files])

    manifest = {
        "format_version": BUNDLE_FORMAT_VERSION,
        "exported_at": datetime.datetime.now().astimezone().isoformat(),
        "exporter": "memory-server export",
        "source_dirs": src_dirs,
        "namespaces": ns_stats,
        "total_files": len(files),
        "total_bytes": total_bytes,
    }
    export_info = {
        "language_profile": lang,
        "completeness_note": "导出 = 源目录全部 .md/.jsonl/.json/.txt 文件；"
                             "如需 100% 迁移请确认源目录即旧系统完整原始层",
    }

    # 打包
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    checksum_lines = []
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2))
        zf.writestr(f"{META_DIR}/{EXPORT_INFO_NAME}",
                    json.dumps(export_info, ensure_ascii=False, indent=2))
        for abs_path, zip_rel in files:
            zf.write(abs_path, zip_rel)
            checksum_lines.append(f"{sha256_file(abs_path)}  {zip_rel}  {os.path.getsize(abs_path)}")
        zf.writestr(CHECKSUMS_NAME, "\n".join(sorted(checksum_lines)) + "\n")

    dt = time.time() - t0
    print(f"📦 记忆包已导出: {out_path}")
    print(f"   文件 {manifest['total_files']} 个 / {total_bytes/1024/1024:.1f} MB / 耗时 {dt:.1f}s")
    for ns, s in sorted(ns_stats.items()):
        print(f"   {ns}: {s['file_count']} 文件 / {s['bytes']/1024:.0f} KB")
    print(f"   语言分布（信息性）: {lang}")
    return {"file_count": manifest["total_files"], "bytes": total_bytes,
            "namespaces": ns_stats, "out": out_path}


# ---------------------------------------------------------------- 导入

def _verify_checksums(zip_path, tmp_dir):
    """校验 zip 内文件 hash 与 CHECKSUMS.txt 一致。

    返回: {"ok": [(zip_rel, size)], "corrupt": [(zip_rel, err)], "unlisted": [...]}
    """
    result = {"ok": [], "corrupt": [], "unlisted": []}
    checksums = {}
    with zipfile.ZipFile(zip_path) as zf:
        if CHECKSUMS_NAME not in zf.namelist():
            return None  # 不是记忆包
        for line in zf.read(CHECKSUMS_NAME).decode("utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("  ")
            if len(parts) == 3:
                checksums[parts[1]] = (parts[0], int(parts[2]))

        for name in zf.namelist():
            if name.startswith(RAW_PREFIX + "/") and not name.endswith("/"):
                # 审计四审安全🔴（2026-08-11）：zip slip 防护——解压目标必须落在 tmp_dir 内，
                # 恶意记忆包含 raw/../../xxx 条目可写任意路径，realpath 校验拒绝
                target = os.path.realpath(os.path.join(tmp_dir, name))
                if not target.startswith(os.path.realpath(tmp_dir) + os.sep):
                    result["corrupt"].append((name, "路径穿越拒绝（zip slip）"))
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(name) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                h = sha256_file(target)
                if name in checksums:
                    if h == checksums[name][0]:
                        result["ok"].append(name)
                    else:
                        result["corrupt"].append((name, "hash 不一致"))
                        os.remove(target)  # 损坏文件不落库，避免被导入
                else:
                    result["unlisted"].append(name)
    return result


def import_bundle(zip_path, provider, dry_run=False):
    """导入记忆包：校验 → 解压 → 按 namespace 导入 → 完整性审计报告

    返回: {"total": n, "imported": n, "skipped": n, "failed": n,
           "corrupt": [...], "by_namespace": {...}, "report": str}
    """
    if not os.path.isfile(zip_path):
        print(f"❌ 记忆包不存在: {zip_path}")
        return None

    tmp_dir = tempfile.mkdtemp(prefix="mem-bundle-")
    try:
        # 1. 校验 manifest
        with zipfile.ZipFile(zip_path) as zf:
            if MANIFEST_NAME not in zf.namelist():
                print("❌ 不是有效的记忆包（缺 manifest.json）")
                return None
            manifest = json.loads(zf.read(MANIFEST_NAME).decode("utf-8"))
        if manifest.get("format_version") != BUNDLE_FORMAT_VERSION:
            print(f"⚠️ 格式版本不匹配: 包={manifest.get('format_version')} "
                  f"本工具={BUNDLE_FORMAT_VERSION}（尝试继续）")

        # 2. hash 校验 + 解压
        print("🔒 校验 CHECKSUMS（sha256）...")
        verify = _verify_checksums(zip_path, tmp_dir)
        if verify is None:
            print("❌ 不是有效的记忆包（缺 CHECKSUMS.txt）")
            return None
        print(f"   ✅ 校验通过 {len(verify['ok'])} 文件"
              + (f" | ❌ 损坏 {len(verify['corrupt'])}" if verify["corrupt"] else "")
              + (f" | ⚠️ 未列入清单 {len(verify['unlisted'])}" if verify["unlisted"] else ""))

        # 3. 按 namespace 导入
        raw_root = os.path.join(tmp_dir, RAW_PREFIX)
        namespaces = sorted(d for d in os.listdir(raw_root)
                            if os.path.isdir(os.path.join(raw_root, d)))
        if not namespaces:
            print("❌ 记忆包内无 raw/ 内容")
            return None

        print(f"\n📥 导入记忆包: {os.path.basename(zip_path)}"
              f"（{manifest.get('total_files', '?')} 文件 / {len(namespaces)} namespace）"
              + (" [dry-run]" if dry_run else ""))

        by_ns = {}
        total = {"imported": 0, "skipped": 0, "failed": 0, "source": 0}
        for ns in namespaces:
            src = os.path.join(raw_root, ns)
            r = import_dir(src, ns, provider, dry_run=dry_run)
            by_ns[ns] = r
            total["source"] += r["ok"] + r["skipped"] + len(r["failed"])
            total["imported"] += r["ok"]
            total["skipped"] += r["skipped"]
            total["failed"] += len(r["failed"])
            status = "✅" if not r["failed"] else "❌"
            print(f"   {status} [{ns}] 源 {r['ok'] + r['skipped'] + len(r['failed'])} "
                  f"→ 导入 {r['ok']} / 跳过 {r['skipped']} / 失败 {len(r['failed'])}")

        # 4. 完整性审计报告
        missing = [p for p, _ in verify["corrupt"]] + verify["unlisted"]
        print("\n📋 完整性审计报告")
        print(f"   包内文件（hash 通过）: {len(verify['ok'])}")
        if verify["corrupt"]:
            print(f"   ❌ 损坏（未导入）: {len(verify['corrupt'])}")
            for p, err in verify["corrupt"][:10]:
                print(f"      {p}: {err}")
        if verify["unlisted"]:
            print(f"   ⚠️ 未列入 CHECKSUMS（已导入但无法校验）: {len(verify['unlisted'])}")
        print(f"   已摄入: {total['imported']} / 跳过(重复): {total['skipped']} / "
              f"失败: {total['failed']}")
        if total["failed"]:
            print("   ❌ 失败清单（前 10 条）:")
            for ns, r in by_ns.items():
                for path, err in r["failed"][:10]:
                    print(f"      {path}: {err}")

        if not total["failed"] and not verify["corrupt"]:
            print("\n✅ 迁移完整：原始层完整前提下零缺失")
        else:
            print("\n⚠️ 存在缺口：上方清单为缺口明细，可人工补齐后重跑（幂等，不翻倍）")

        return {"total": len(verify["ok"]), "imported": total["imported"],
                "skipped": total["skipped"], "failed": total["failed"],
                "corrupt": [p for p, _ in verify["corrupt"]],
                "by_namespace": by_ns}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------- CLI

def main_export(argv=None):
    import argparse
    parser = argparse.ArgumentParser(prog="eidetic export", description="导出记忆包（旧系统原始层 → memories.zip）")
    parser.add_argument("--source", action="append", required=True,
                        help="源目录（可多次，如 memory/dialogue、memory/core、.learnings）")
    parser.add_argument("--out", default="memories.zip", help="输出 zip 路径（默认 memories.zip）")
    args = parser.parse_args(argv)
    result = export_bundle(args.source, args.out)
    return 0 if result else 1


def main_import_bundle(argv=None):
    import argparse
    parser = argparse.ArgumentParser(prog="eidetic import-bundle", description="导入记忆包（memories.zip → 新系统）")
    parser.add_argument("--bundle", required=True, help="记忆包 zip 路径")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写入")
    parser.add_argument("--fts-only", action="store_true", help="仅关键词模式")
    parser.add_argument("--backend", default=None, choices=["llama.cpp", "ollama", "fts-only"],
                        help="嵌入后端（默认自动；大批量建议 ollama，快~6倍）")
    args = parser.parse_args(argv)

    from memory_server import db
    db.init_db()
    if args.dry_run:
        provider = None
    else:
        from memory_server.embed import detect_provider
        provider = detect_provider(prefer=args.backend or ("fts-only" if args.fts_only else None))
    result = import_bundle(args.bundle, provider, dry_run=args.dry_run)
    return 0 if result and not result["failed"] and not result["corrupt"] else 1


if __name__ == "__main__":
    sys.exit(main_export())
