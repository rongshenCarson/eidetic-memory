#!/usr/bin/env python3
"""
memory-server 语义去重（②a，MemPalace dedup.py 借鉴）
========================================================
相似内容不重复入库：cosine 距离 < 阈值视为重复，保留更长/更丰富的一条。

对标 MemPalace dedup.py：DEFAULT_THRESHOLD = 0.15（≈85% 相似）。
与 ingested_hashes（精确 hash 去重）互补：hash 去重挡完全相同的，
语义去重挡「意思一样但表述不同」的。

用法:
  eidetic dedup [--ns brand] [--threshold 0.15] [--dry-run] [--limit 5000]

安全：
  - 默认 dry-run（只报告不删除）
  - 删除走 DELETE FROM chunks → FTS trigger 自动同步
  - 保留策略：文本更长优先（信息量更多），其次 updated_at 更新优先
"""
import os
import sys
import time
import argparse
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory_server import db  # noqa: E402
from memory_server.search import cosine  # noqa: E402

log = logging.getLogger("memory-server.dedup")

DEFAULT_THRESHOLD = 0.15  # cosine DISTANCE（与 MemPalace 一致）
# 审计🔵修复（2026-08-11）：单组向量矩阵护栏——n 条 → n²×4B 瞬态内存，
# 2.3 万条组 ≈ 2.1GB，8GB 机器有 OOM 风险。超过上限的组只处理最新 N 条（其余下轮）。
MAX_GROUP_SIZE = 6000    # 6000²×4B ≈ 144MB，安全余量


def _grouped_rows(namespace=None, limit=50000, offset_ns=None, offset_id=0):
    """取候选 chunks（按 namespace 分组，组内两两比较控制复杂度）

    2026-08-10 修复：昨晚误改为按 source_id 分组 → 同文件相邻 chunk
    （滑动窗口重叠）被误判为重复；恢复按 namespace 分组 + 比较时排除
    同 source 相邻 chunk。
    审计六审 P2：offset 轮转——offset_ns/offset_id 提供后，从该游标继续扫
    （(ns, id) 字典序轮转），覆盖全库而非只最新 N 条。
    """
    conn = db.get_conn()
    try:
        sql = ("SELECT id, namespace, source_id, start_line, text, embedding, "
               "LENGTH(text) AS tlen FROM chunks")
        params = []
        conds = []
        if namespace:
            conds.append("namespace=?")
            params.append(namespace)
        if offset_ns is not None:
            conds.append("(namespace > ? OR (namespace = ? AND id < ?))")
            params += [offset_ns, offset_ns, offset_id]
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        # 2026-08-11 修复：按 updated_at 倒序（最新摄入优先去重），
        # 原按 id 正序导致永远只扫最早的 N 条，新数据从不被清理
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        groups = {}
        for r in rows:
            groups.setdefault(r["namespace"], []).append(r)
        return groups
    finally:
        conn.close()


def dedup(namespace=None, threshold=DEFAULT_THRESHOLD, dry_run=True, limit=5000,
          offset_ns=None, offset_id=0):
    """语义去重：组内两两比较，相似对保留更长者。

    numpy 向量化（2026-08-11）：整组一次矩阵乘法算相似度矩阵，
    替代纯 Python 两两循环（大库提速 ~100x）。
    审计六审 P2：新增 offset 轮转——全库 >100 万后只扫最新 N 条旧数据重复永不收敛，
    按 (namespace, id) 轮转扫描可覆盖全库（调度任务维护轮转游标）。

    返回: {"scanned": n, "duplicates": n, "removed": n, "pairs": [(keep_id, drop_id, dist)]}
    """
    groups = _grouped_rows(namespace, limit, offset_ns, offset_id)
    scanned = sum(len(v) for v in groups.values())
    pairs = []
    try:
        import numpy as np
        HAVE_NP = True
    except Exception:
        HAVE_NP = False

    for ns, rows in groups.items():
        n = len(rows)
        if n < 2:
            continue
        # 审计🔵护栏：超大组只取最新 MAX_GROUP_SIZE 条（_grouped_rows 已按 updated_at DESC）
        if n > MAX_GROUP_SIZE:
            rows = rows[:MAX_GROUP_SIZE]
            n = MAX_GROUP_SIZE
        # 长度差 >30% 快速排除（不可能是语义重复）
        keep_idx = []
        for i in range(n):
            r = rows[i]
            if r["embedding"]:
                keep_idx.append(i)
        if len(keep_idx) < 2:
            continue

        if HAVE_NP:
            # 向量化：组内嵌入矩阵 → 余弦相似度矩阵
            vecs = []
            for i in keep_idx:
                v = db.blob_decode(rows[i]["embedding"])
                vecs.append(v if v is not None else None)
            valid = [i for i, v in zip(keep_idx, vecs) if v is not None]
            if len(valid) < 2:
                continue
            valid_vecs = np.array([db.blob_decode(rows[i]["embedding"]) for i in valid],
                                  dtype=np.float32)
            norms = np.linalg.norm(valid_vecs, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            sim = (valid_vecs @ valid_vecs.T) / (norms @ norms.T)
            vidx = {orig: k for k, orig in enumerate(valid)}
            for a_i in range(len(valid)):
                for b_j in range(a_i + 1, len(valid)):
                    a, b = rows[valid[a_i]], rows[valid[b_j]]
                    # 同 source（同一文档的滑动窗口切片）→ 跳过
                    if a["source_id"] == b["source_id"]:
                        continue
                    dist = 1.0 - float(sim[a_i][b_j])
                    if dist < threshold:
                        # 保留更长者（信息量多），其次更新者
                        if a["tlen"] >= b["tlen"]:
                            keep, drop = a, b
                        else:
                            keep, drop = b, a
                        pairs.append((keep["id"], drop["id"], round(dist, 4), ns))
        else:
            # 降级：纯 Python 两两
            vecs = {}
            for r in rows:
                vecs[r["id"]] = db.blob_decode(r["embedding"]) if r["embedding"] else None
            for i in range(n):
                for j in range(i + 1, n):
                    a, b = rows[i], rows[j]
                    if a["source_id"] == b["source_id"]:
                        continue
                    if abs(a["tlen"] - b["tlen"]) / max(a["tlen"], b["tlen"], 1) > 0.3:
                        continue
                    va, vb = vecs[a["id"]], vecs[b["id"]]
                    if va is None or vb is None or len(va) != len(vb):
                        continue
                    dist = 1.0 - cosine(va, vb)
                    if dist < threshold:
                        if a["tlen"] >= b["tlen"]:
                            keep, drop = a, b
                        else:
                            keep, drop = b, a
                        pairs.append((keep["id"], drop["id"], round(dist, 4), ns))

    # 执行删除（去重：同一 drop 只删一次）
    removed_ids = set()
    for keep_id, drop_id, dist, ns in pairs:
        if drop_id in removed_ids:
            continue
        removed_ids.add(drop_id)

    removed = 0
    if not dry_run and removed_ids:
        conn = db.get_conn()
        try:
            placeholders = ",".join("?" * len(removed_ids))
            cur = conn.execute(f"DELETE FROM chunks WHERE id IN ({placeholders})",
                               list(removed_ids))
            removed = cur.rowcount
            conn.commit()
        finally:
            conn.close()
    elif dry_run:
        removed = len(removed_ids)

    return {"scanned": scanned, "duplicates": len(pairs), "removed": removed,
            "pairs": pairs[:20], "dry_run": dry_run}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="eidetic dedup", description="语义去重（相似内容合并）")
    parser.add_argument("--ns", default=None, help="限定 namespace")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help=f"cosine 距离阈值（默认 {DEFAULT_THRESHOLD} ≈ 85% 相似）")
    parser.add_argument("--dry-run", action="store_true", help="只报告不删除（默认）")
    parser.add_argument("--apply", action="store_true", help="实际删除（配合 --dry-run 互斥）")
    parser.add_argument("--limit", type=int, default=5000)
    args = parser.parse_args(argv)

    db.init_db()
    dry = not args.apply  # 默认 dry-run；--apply 才真删
    r = dedup(namespace=args.ns, threshold=args.threshold, dry_run=dry, limit=args.limit)
    print(f"🔍 语义去重: 扫描 {r['scanned']} 条 / 相似对 {r['duplicates']} / "
          f"{'将删除' if dry else '已删除'} {r['removed']} 条"
          + ("（dry-run，--apply 执行）" if dry else ""))
    for keep, drop, dist, ns in r["pairs"][:10]:
        print(f"  保留 {keep} / 删除 {drop} (dist={dist}, ns={ns})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
