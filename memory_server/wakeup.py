#!/usr/bin/env python3
"""
memory-server wake-up 分层唤醒上下文（②c，MemPalace layers.py 借鉴）
======================================================================
对标 MemoryStack.wake_up：L0 身份 + L1 精华故事，600-900 tokens 唤醒上下文。
目标：会话开始时用最少 token 给 agent 最重要的记忆（95% 上下文留给对话）。

数据源（对齐 Eidetic 各层）：
  L0 身份   → persona 表（L3 画像）
  L1 精华   → curated.core_summary（核心归集）+ core_memories（晋升关键词）
              + extracts 高重要性（importance>=4）

用法:
  eidetic wake-up [--ns brand] [--max-tokens 900]
"""
import os
import sys
import json
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory_server import db  # noqa: E402


def _tokens(text):
    """中文估算：1 字符 ≈ 1 token（英文 4 字符 ≈ 1 token）"""
    zh = sum(1 for c in text if ord(c) > 0x2E80)
    en = len(text) - zh
    return zh + en // 4


def wake_up(namespace=None, max_tokens=900):
    """生成唤醒上下文。返回 {"text": str, "tokens": n, "parts": {...}}

    2026-08-10 修复：namespace 默认 None → 全库查询（数据分布在
    dialogue/core 等 namespace，默认 default 导致唤醒上下文全空）
    """
    conn = db.get_conn()
    try:
        # L0: 画像（全库取最新）
        persona = None
        if namespace:
            row = conn.execute(
                "SELECT content FROM persona WHERE namespace=? LIMIT 1",
                (namespace,)).fetchone()
        else:
            row = conn.execute(
                "SELECT content FROM persona ORDER BY updated_at DESC LIMIT 1").fetchone()
        if row:
            persona = row[0]

        # L1a: curated 核心归集（全库取最新）
        curated = None
        if namespace:
            row = conn.execute(
                "SELECT content FROM curated WHERE namespace=? ORDER BY updated_at DESC LIMIT 1",
                (namespace,)).fetchone()
        else:
            row = conn.execute(
                "SELECT content FROM curated ORDER BY updated_at DESC LIMIT 1").fetchone()
        if row:
            curated = row[0]

        # L1b: 晋升关键词（全库 top8）
        if namespace:
            kws = conn.execute(
                "SELECT keyword, freq FROM core_memories WHERE namespace=? ORDER BY freq DESC LIMIT 8",
                (namespace,)).fetchall()
        else:
            kws = conn.execute(
                "SELECT keyword, freq FROM core_memories ORDER BY freq DESC LIMIT 8").fetchall()

        # L1c: 高重要性提取物（G3 修复 2026-08-11：按类型配额各取 top2，
        # 原全库 top10 被开发期同一类记录垄断，业务记忆被挤出）
        if namespace:
            top = conn.execute(
                "SELECT type, short, text, importance FROM extracts WHERE namespace=? "
                "AND importance>=4 AND id IN ("
                "  SELECT id FROM extracts e2 WHERE e2.namespace=extracts.namespace "
                "  AND e2.type=extracts.type AND e2.importance>=4 "
                "  ORDER BY e2.importance DESC, e2.id DESC LIMIT 2) "
                "ORDER BY importance DESC, id DESC LIMIT 10",
                (namespace,)).fetchall()
        else:
            top = conn.execute(
                "SELECT type, short, text, importance FROM extracts "
                "WHERE importance>=4 AND id IN ("
                "  SELECT id FROM extracts e2 WHERE e2.type=extracts.type "
                "  AND e2.importance>=4 "
                "  ORDER BY e2.importance DESC, e2.id DESC LIMIT 2) "
                "ORDER BY importance DESC, id DESC LIMIT 10").fetchall()
    finally:
        conn.close()

    parts = {}
    lines = []
    # L0
    if persona:
        parts["L0_persona"] = persona
        lines.append("## L0 — 身份画像")
        lines.append(persona[:600])
        lines.append("")
    # L1
    l1 = []
    if curated:
        parts["L1_curated"] = curated
        l1.append(f"- 核心归集: {curated[:400]}")
    if kws:
        parts["L1_keywords"] = [r["keyword"] for r in kws]
        l1.append("- 高频主题: " + "、".join(r["keyword"] for r in kws))
    for r in top:
        parts.setdefault("L1_extracts", []).append(r["short"] or r["text"][:80])
        l1.append(f"- [{r['type']}] {(r['short'] or r['text'])[:120]} (★{r['importance']})")
    if l1:
        lines.append("## L1 — 精华记忆")
        lines.extend(l1)
        lines.append("")

    text = "\n".join(lines)
    # token 截断（保留开头精华）
    if _tokens(text) > max_tokens:
        budget = max_tokens - _tokens(lines[0]) - 20
        cut = []
        used = 0
        for line in lines[2:]:
            t = _tokens(line)
            if used + t > budget:
                break
            cut.append(line)
            used += t
        text = "\n".join(lines[:2] + cut)
    return {"text": text, "tokens": _tokens(text),
            "parts": {k: (v if isinstance(v, str) else v) for k, v in parts.items()}}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="eidetic wake-up", description="唤醒上下文（L0 身份 + L1 精华）")
    # 审计🟡修复（2026-08-11）：默认改 None 全库——persona/curated 数据分布在
    # 各 namespace（含历史 NULL/'' 行），默认 default 导致 L0 画像查空
    parser.add_argument("--ns", default=None)
    parser.add_argument("--max-tokens", type=int, default=900)
    args = parser.parse_args(argv)
    db.init_db()
    r = wake_up(namespace=args.ns, max_tokens=args.max_tokens)
    print(r["text"])
    print(f"\n--- {r['tokens']} tokens / {args.max_tokens} 上限 ---")
    return 0


if __name__ == "__main__":
    sys.exit(main())
