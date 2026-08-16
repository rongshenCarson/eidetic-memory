#!/usr/bin/env python3
"""
memory-server 沉底/归档（P1a）
==============================
对应现有系统的「symlink 沉底」：旧内容移出活跃检索区，仍可深检索。

实现：chunks.metadata 标记 archived=true + namespace 前缀 `archived:`，
检索时默认排除 archived，加 --include-archived 可深检索。
"""
import time
import json
import logging

from . import db

log = logging.getLogger("memory-server.archive")

ARCHIVE_DAYS = 90  # 90 天前的内容沉底（对齐现有 core 90 天归档策略）


def archive_old(days=ARCHIVE_DAYS, dry_run=True):
    """将超过 days 天的 chunk 标记为已沉底"""
    cutoff = time.time() - days * 86400
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT id, metadata FROM chunks WHERE updated_at < ? AND metadata NOT LIKE '%archived%'",
            (cutoff,),
        ).fetchall()
        if dry_run:
            conn.close()
            log.info(f"📦 沉底预检: {len(rows)} 个旧 chunk 将被沉底（{days} 天前）")
            return len(rows)
        for r in rows:
            try:
                meta = json.loads(r["metadata"])
            except Exception:
                meta = {}
            meta["archived"] = True
            conn.execute("UPDATE chunks SET metadata=? WHERE id=?", (json.dumps(meta), r["id"]))
        conn.commit()
        log.info(f"📦 沉底完成: {len(rows)} 个 chunk 标记 archived")
        return len(rows)
    finally:
        conn.close()
