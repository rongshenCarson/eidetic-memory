#!/usr/bin/env python3
"""
memory-server 自动分类器（F3，完整版 2026-08-09）
====================================================
对标 MemPalace wing/room 自动分类能力：
  - wing（项目/域）→ namespace（已有：agent 维度）
  - room（内容方面）→ 自动打标：主题（topic）+ 内容形态（type）

双轨：
  1. 规则分类（零依赖，秒级）——主题关键词规则
  2. LLM 分类（可选）——精确主题+形态判断

room 标签写入 chunks.metadata（JSON）的 room 字段，检索可按 room 过滤。
"""
import os
import sys
import json
import time
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory_server import db  # noqa: E402

log = logging.getLogger("memory-server.classifier")

# 主题规则（关键词 → 主题），可扩展
TOPIC_RULES = {
    "品牌推广": ["林下有", "云茯苓", "祛湿茶", "品牌", "推广", "达人", "电商", "直播", "货架", "快懂百科"],
    "技术系统": ["脚本", "代码", "模型", "架构", "部署", "数据库", "bug", "修复", "接口", "测试", "配置", "嵌入"],
    "项目管理": ["方案", "计划", "进度", "迭代", "里程碑", "排期", "复盘", "决策", "任务"],
    "健康养生": ["养生", "药食", "饮食", "运动", "作息", "茯苓", "体质", "调理"],
    "学习成长": ["学习", "读书", "课程", "笔记", "研究", "调研", "方法论"],
    "生活日常": ["旅行", "天气", "美食", "购物", "家庭", "朋友"],
}

TYPE_RULES = {
    "decision": ["决定", "定稿", "采用", "选择", "方案", "确认"],
    "milestone": ["完成", "上线", "发布", "里程碑", "版本"],
    "fact": ["是", "属于", "位于", "成立于", "有"],
    "preference": ["偏好", "喜欢", "习惯", "倾向"],
}


def rule_classify_topic(text):
    """规则主题分类：命中得分最高的主题"""
    best, best_score = "其他", 0
    for topic, kws in TOPIC_RULES.items():
        score = sum(1 for kw in kws if kw in text)
        if score > best_score:
            best, best_score = topic, score
    return best


def rule_classify_type(text):
    """规则内容形态分类"""
    for ftype, kws in TYPE_RULES.items():
        if any(kw in text for kw in kws):
            return ftype
    return "episodic"


def classify_text(text):
    """双标签分类：{topic, type}"""
    return {"topic": rule_classify_topic(text), "type": rule_classify_type(text)}


def auto_classify(namespace=None, limit=200):
    """对缺失 room 标签的 chunks 自动分类（幂等：已有标签跳过）。

    返回: {"classified": n, "skipped": n}
    """
    conn = db.get_conn()
    try:
        # G1 修复（2026-08-11）：优先未打标（原实现 ORDER BY id DESC 总是取最新
        # limit 条，旧数据永远覆盖不到）
        if namespace:
            rows = conn.execute(
                "SELECT id, text, metadata FROM chunks WHERE namespace=? "
                "AND metadata NOT LIKE '%\"room\"%' ORDER BY id LIMIT ?",
                (namespace, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, text, metadata FROM chunks "
                "WHERE metadata NOT LIKE '%\"room\"%' ORDER BY id LIMIT ?",
                (limit,)).fetchall()
        n = 0
        for cid, text, meta in rows:
            meta = json.loads(meta or "{}")
            if meta.get("room"):
                continue  # 已有标签，跳过
            room = classify_text(text)
            meta["room"] = room
            conn.execute("UPDATE chunks SET metadata=? WHERE id=?",
                         (json.dumps(meta, ensure_ascii=False), cid))
            n += 1
        conn.commit()
        return {"classified": n}
    finally:
        conn.close()


def room_stats(namespace=None):
    """room 分布统计"""
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT metadata FROM chunks" + (" WHERE namespace=?" if namespace else ""),
            ([namespace] if namespace else [])).fetchall()
        topics, types = {}, {}
        for (meta,) in rows:
            try:
                room = json.loads(meta or "{}").get("room") or {}
            except Exception:
                continue
            t = room.get("topic", "未分类")
            topics[t] = topics.get(t, 0) + 1
            ty = room.get("type", "未知")
            types[ty] = types.get(ty, 0) + 1
        return {"topics": topics, "types": types}
    finally:
        conn.close()


def main():
    import argparse
    parser = argparse.ArgumentParser(prog="eidetic classify", description="自动分类（room 打标）")
    parser.add_argument("--ns", default=None)
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args()
    db.init_db()
    r = auto_classify(namespace=args.ns, limit=args.limit)
    print(f"🏷️  自动分类完成: {r['classified']} 条新增标签")
    print("分布:", room_stats(namespace=args.ns))
    return 0


if __name__ == "__main__":
    sys.exit(main())
