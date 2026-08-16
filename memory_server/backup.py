#!/usr/bin/env python3
"""
memory-server 自动备份（#37，2026-08-10 补）
=============================================
定期备份 memory.db + raw 清单到 backups/，保留最近 N 份。
备份保留策略（最近 N 份自动轮转）。

用法:
  eidetic backup [--keep 7]          # 手动备份
  （scheduler 任务每日自动执行）
"""
import os
import sys
import time
import glob
import shutil
import sqlite3
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory_server import db  # noqa: E402

# 备份目录：跟随 MEMORY_SERVER_DB_DIR 的同级目录（与 vector_index/doctor/service 同模式，
# import 前由 env 确定，不依赖可变全局；默认包内 backups/ 行为不变）
_DATA_OVERRIDE = os.environ.get("MEMORY_SERVER_DB_DIR")
if _DATA_OVERRIDE:
    BACKUP_DIR = os.path.join(os.path.dirname(_DATA_OVERRIDE), "backups")
else:
    BACKUP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backups")


def backup(keep=7, out_dir=None, verify=True, light=False):
    """备份 memory.db。返回 {"path": str, "size_mb": n, "pruned": n}

    verify: 备份后自动校验完整性（2026-08-11 Y4：坏备份不保留，避免安静躺尸）
    light: 轻备份（审计六审 P1，2026-08-11）——备份后剔除 embedding 列（向量 368MB/816MB≈45%，
           可由文本重嵌再生）；体积约砍半，恢复后需 reembed。100 万级 7 份备份 31.5GB→~17GB。
    审计🟡修复（2026-08-11）：shutil.copy2 直接拷贝 WAL 活库主文件 → 不含 -wal 未 checkpoint
    数据（最近事务可丢）。改用 sqlite3 在线备份 API（热备，一致性快照）。
    """
    out_dir = out_dir or BACKUP_DIR
    os.makedirs(out_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(out_dir, f"memory_{ts}.db")
    # 在线热备：sqlite3 backup API 读一致性快照（含 WAL 未 checkpoint 事务）
    try:
        src = sqlite3.connect(db.DB_PATH)
        dst = sqlite3.connect(dest)
        src.backup(dst)
        if light:
            # 轻备份：剔除 embedding 列（向量可重嵌再生，体积砍半）
            try:
                dst.execute("UPDATE chunks SET embedding=NULL")
                dst.commit()
                dst.execute("VACUUM")
            except Exception as e:
                print(f"⚠️ 轻备份剔向量失败（保留全量）: {e}")
        dst.close()
        src.close()
    except Exception as e:
        # 兜底：备份 API 失败才用 copy2（至少留一份，校验会拦坏文件）
        try:
            shutil.copy2(db.DB_PATH, dest)
        except Exception as e2:
            return {"path": None, "size_mb": 0, "pruned": 0,
                    "error": f"备份失败（backup API: {e}; copy2: {e2}）"}
    # Y4 校验：PRAGMA integrity_check + chunks 计数（对齐源库）
    if verify:
        try:
            vconn = sqlite3.connect(dest)
            integrity = vconn.execute("PRAGMA integrity_check").fetchone()[0]
            n = vconn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            vconn.close()
            src_n = db.get_conn().execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            db.get_conn().close()
            if integrity != "ok" or n < max(1, src_n * 0.9):
                os.remove(dest)
                return {"path": None, "size_mb": 0, "pruned": 0,
                        "error": f"备份校验失败（integrity={integrity}, chunks={n}, 源={src_n}）已丢弃"}
        except Exception as e:
            os.remove(dest)
            return {"path": None, "size_mb": 0, "pruned": 0,
                    "error": f"备份校验异常（{e}）已丢弃"}
    # 清理旧备份（保留 keep 份）
    old = sorted(glob.glob(os.path.join(out_dir, "memory_*.db")))
    pruned = 0
    while len(old) > keep:
        os.remove(old.pop(0))
        pruned += 1
    return {"path": dest, "size_mb": round(os.path.getsize(dest) / 1024 / 1024, 1),
            "pruned": pruned}


def list_backups(out_dir=None):
    """列出可用备份（按时间倒序）"""
    out_dir = out_dir or BACKUP_DIR
    if not os.path.isdir(out_dir):
        return []
    files = sorted(glob.glob(os.path.join(out_dir, "memory_*.db")), reverse=True)
    out = []
    for f in files:
        out.append({"path": f, "size_mb": round(os.path.getsize(f) / 1024 / 1024, 1),
                    "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(f)))})
    return out


def restore(target=None, out_dir=None, keep_index=False):
    """从备份恢复 memory.db（可选连同向量索引）。

    target: 备份文件路径；None = 最新一份
    返回 {"restored": str, "chunks": n}
    注意：恢复后 chunks 与当前 USearch 索引可能不一致 → 建议重建索引（doctor 会提示漂移）
    """
    backups = list_backups(out_dir)
    if not backups:
        raise FileNotFoundError(f"无备份可用（{out_dir or BACKUP_DIR}）")
    if target is None:
        target = backups[0]["path"]
    elif not os.path.isabs(target):
        # 支持传文件名 memory_20260810_221443.db
        cand = os.path.join(out_dir or BACKUP_DIR, target)
        if os.path.exists(cand):
            target = cand
    if not os.path.exists(target):
        raise FileNotFoundError(f"备份不存在: {target}")

    # 备份当前库（恢复前保险）
    safety = backup(keep=7)
    shutil.copy2(target, db.DB_PATH)

    # 向量索引：恢复后与库不一致 → 删除旧索引让 ensure_index 重建（避免错误命中）
    from memory_server.vector_index import INDEX_PATH
    if os.path.exists(INDEX_PATH) and not keep_index:
        os.remove(INDEX_PATH)

    conn = db.get_conn()
    try:
        n = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
        # 审计六审 P1：轻备份恢复检测——embedding 为 NULL 的行数（恢复后需 reembed）
        null_emb = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE embedding IS NULL").fetchone()[0]
    finally:
        conn.close()
    return {"restored": target, "chunks": n, "integrity": integrity,
            "null_embeddings": null_emb,
            "note": ("轻备份（向量已剔除），恢复后运行 eidetic maintain reembed 重建向量"
                     if null_emb and null_emb == n else None),
            "safety_backup": safety["path"]}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="eidetic backup", description="数据库备份/恢复")
    sub = parser.add_subparsers(dest="cmd")
    p_b = sub.add_parser("create", help="创建备份")
    p_b.add_argument("--keep", type=int, default=7)
    p_l = sub.add_parser("list", help="列出备份")
    p_r = sub.add_parser("restore", help="恢复备份")
    p_r.add_argument("target", nargs="?", default=None, help="备份文件名/路径（默认最新）")
    p_r.add_argument("--keep-index", action="store_true", help="保留现有向量索引（不推荐）")
    args = parser.parse_args(argv)
    db.init_db()
    if args.cmd == "create" or args.cmd is None:
        r = backup(keep=args.keep if args.cmd == "create" else 7)
        print(f"💾 备份完成: {r['path']} ({r['size_mb']}MB)")
    elif args.cmd == "list":
        bs = list_backups()
        if not bs:
            print("📭 无备份")
        else:
            print(f"📦 备份 {len(bs)} 份:")
            for i, b in enumerate(bs):
                print(f"  [{i}] {b['mtime']} {b['size_mb']}MB {os.path.basename(b['path'])}")
    elif args.cmd == "restore":
        r = restore(target=args.target, keep_index=args.keep_index)
        print(f"♻️  已恢复: {os.path.basename(r['restored'])}")
        print(f"   chunks: {r['chunks']} | integrity: {r['integrity']}")
        print(f"   恢复前已自动备份当前库: {os.path.basename(r['safety_backup'])}")
        print(f"   向量索引已重置（下次检索自动重建）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
