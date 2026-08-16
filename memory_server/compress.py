#!/usr/bin/env python3
"""
memory-server AAAK 结构化压缩（②b，MemPalace dialect.py 借鉴）
================================================================
对标 MemPalace AAAK 方言的规则压缩：实体编码 + 主题 + 关键句 + 情绪 + 标志。

与 extracts.short（LLM 语义压缩）互补：
  - short: LLM 提炼的一句话（语义准，需 LLM）
  - AAAK: 规则结构化（零依赖、快、机械稳定），供检索注入/审计用

用法:
  eidetic compress [--ns brand] [--limit 500]   # 为缺失的 chunks 生成 AAAK 存 extracts
  eidetic compress --text "..."                 # 单条预览
"""
import os
import sys
import re
import json
import time
import argparse
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory_server import db  # noqa: E402

log = logging.getLogger("memory-server.compress")

EMOTION_POS = ["满意", "很好", "不错", "成功", "搞定", "完成", "顺利", "棒", "赞", "完美", "开心", "放心"]
EMOTION_NEG = ["问题", "失败", "报错", "错误", "卡住", "崩溃", "不满", "担心", "危险", "坑", "不行", "挂了"]
FLAG_KEYWORDS = {
    "decision": ["决定", "定稿", "采用", "选择", "方案", "确认", "拍板"],
    "milestone": ["完成", "上线", "发布", "里程碑", "版本", "交付"],
    "fact": ["是", "属于", "位于", "成立于", "拥有", "位于"],
    "preference": ["偏好", "喜欢", "习惯", "倾向", "更愿意"],
    "issue": ["问题", "报错", "bug", "失败", "卡住", "崩溃"],
}
KEY_SENTENCE_WEIGHTS = [
    (r"结论|决定|定稿|确认|方案|教训|注意|建议|总结", 3),
    (r"问题|报错|bug|失败|错误|修复", 2),
    (r"✅|❌|⚠️|🔴|🟡", 2),
    (r"因为|所以|需要|必须|应该|不[对行能]", 1),
]


def detect_entities(text, known=None):
    """实体检测：已知 KG 实体匹配 + 中文 2-4 字专名（含「公司/品牌/项目/系统」后缀优先）"""
    found = []
    if known:
        for name in known:
            if name and len(name) >= 2 and name in text:
                found.append(name)
    # 规则补：连续 2-4 字 + 常见主体后缀
    for m in re.finditer(r'([\u4e00-\u9fff]{2,6}?(?:品牌|公司|集团|项目|系统|方案|平台|团队|产品))', text):
        w = m.group(1)
        if w not in found:
            found.append(w)
    return found[:6]


def detect_emotion(text):
    pos = sum(1 for w in EMOTION_POS if w in text)
    neg = sum(1 for w in EMOTION_NEG if w in text)
    if pos > neg:
        return "positive"
    if neg > pos:
        return "negative"
    return "neutral"


def detect_flags(text):
    flags = []
    for ftype, kws in FLAG_KEYWORDS.items():
        if any(kw in text for kw in kws):
            flags.append(ftype)
    return flags or ["note"]


def pick_key_sentence(text):
    """关键句选择：按权重打分（对标 MemPalace dialect 句子评分）"""
    sentences = [s.strip() for s in re.split(r'[。！？!?\n]', text) if s.strip()]
    if not sentences:
        return text[:55]
    best, best_score = sentences[0], -1
    for s in sentences:
        score = 0
        for pattern, w in KEY_SENTENCE_WEIGHTS:
            if re.search(pattern, s):
                score += w
        if len(s) > 150:
            score -= 2
        if len(s) < 10:
            score -= 1
        if score > best_score:
            best, best_score = s, score
    return best if len(best) <= 55 else best[:52] + "..."


def compress_text(text, known_entities=None):
    """规则压缩 → AAAK 结构化 dict"""
    topic = "其他"
    try:
        from memory_server.classifier import rule_classify_topic
        topic = rule_classify_topic(text)
    except Exception:
        pass
    return {
        "topic": topic,
        "entities": detect_entities(text, known_entities),
        "key_sentence": pick_key_sentence(text),
        "emotion": detect_emotion(text),
        "flags": detect_flags(text),
    }


def aaak_format(c):
    """格式化为 AAAK 风格字符串"""
    flags = ",".join(c["flags"])
    ents = "|".join(c["entities"]) if c["entities"] else "-"
    return (f"[{flags}] topic:{c['topic']} emotion:{c['emotion']} "
            f"ents:{ents} :: {c['key_sentence']}")


def compress_chunks(namespace=None, limit=500, dry_run=False):
    """为缺失 AAAK 的 chunks 生成压缩并存入 extracts(type=aaak)

    返回: {"generated": n, "scanned": n}
    """
    conn = db.get_conn()
    try:
        sql = "SELECT id, namespace, text FROM chunks"
        params = []
        if namespace:
            sql += " WHERE namespace=?"
            params.append(namespace)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        # 已有 aaak 的 chunk 跳过（幂等）
        done = {r[0] for r in conn.execute(
            "SELECT DISTINCT source_id FROM extracts WHERE type='aaak' AND source_id IS NOT NULL").fetchall()}
        known = [r[0] for r in conn.execute("SELECT name FROM entities").fetchall()]
    finally:
        conn.close()

    generated = 0
    for r in rows:
        if r["id"] in done:
            continue
        c = compress_text(r["text"], known)
        aaak = aaak_format(c)
        if dry_run:
            generated += 1
            continue
        conn = db.get_conn()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO extracts(namespace, type, text, short, importance, "
                "entities, tags, timestamp, source_id, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (r["namespace"], "aaak", aaak, c["key_sentence"], 3,
                 json.dumps(c["entities"], ensure_ascii=False),
                 json.dumps(c["flags"], ensure_ascii=False),
                 time.strftime("%Y-%m-%d"), r["id"], time.time()))
            conn.commit()
        finally:
            conn.close()
        generated += 1
    return {"generated": generated, "scanned": len(rows)}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="eidetic compress", description="AAAK 结构化压缩")
    parser.add_argument("--ns", default=None)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--text", default=None, help="单条预览模式")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.text:
        c = compress_text(args.text)
        print("AAAK:", aaak_format(c))
        print(json.dumps(c, ensure_ascii=False, indent=2))
        return 0

    db.init_db()
    r = compress_chunks(namespace=args.ns, limit=args.limit, dry_run=args.dry_run)
    print(f"🗜️  AAAK 压缩: 扫描 {r['scanned']} / 生成 {r['generated']}"
          + ("（dry-run）" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
