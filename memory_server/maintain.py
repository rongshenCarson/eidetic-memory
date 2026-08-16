#!/usr/bin/env python3
"""
memory-server 维护模块（F5，完整版 2026-08-09）
==================================================
对标旧系统 5 个子脚本，适配单库：

  LRN 教训管理    → add_learning / list_learnings（对标 .learnings/LRN 生命周期）
  curated 同步    → sync_curated（对标 auto_sync_curated：核心内容归集）
  反思            → run_reflection（对标 auto_reflection：每日复盘）
  冲突解决        → scan_conflicts（对标 auto_resolve_conflicts：KG 冲突扫描标记）
  反馈信号        → detect_feedback（对标 auto_feedback_weight：负→教训 / 正→KG 强化）
"""
import os
import re
import sys
import time
import json
import logging
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory_server import db  # noqa: E402

log = logging.getLogger("memory-server.maintain")

# 反馈信号模式（从 auto_feedback_weight.py 搬入，能力等价）
# 审计🟡修复（2026-08-11）：原 \b 词边界对 CJK 无意义（中文间无边界）→ 正/负反馈几乎永不命中。
# 改为纯子串匹配（去 \b），且把中文词放到模式内，英文词保留 \b。
NEGATIVE_PATTERNS = [
    (r'(?:不对|错了|不要|不行|错误|不是|重来|别胡|瞎搞)', "负反馈"),
    (r'(?:搞错了|不是这样|你.*理解.*错|你.*没.*看到|又.*错了|乱来|重做|取消)', "纠正"),
    (r'(?:我不.*让.*你|你.*在.*干嘛|你.*没.*理解)', "意图纠正"),
]
POSITIVE_PATTERNS = [
    (r'(?:对的|对了|好的|就是这样|没问题|可以|很好|完美|正确|正是|不错|就这个|太好了|棒|牛|厉害|可以的|收到|了解|是的|没错)', "正反馈"),
    (r'(?:这个.*很好|很.*到位|总结.*不错|分析.*对|方向.*对)', "正向评价"),
]


# ===== LRN 教训管理 =====

def add_learning(namespace, title, root_cause=None, lesson=None, fix=None,
                 trigger_when=None, trigger_do=None):
    """写入教训（对标 .learnings LRN 格式），code 自动编号 LRN-YYYYMMDD-NNN"""
    # N1 系统性修复（2026-08-11）：namespace=None → 空串，防 NOT NULL 约束静默吞
    namespace = namespace or ""
    conn = db.get_conn()
    try:
        today = datetime.now().strftime("%Y%m%d")
        prefix = f"LRN-{today}-"
        row = conn.execute(
            "SELECT code FROM learnings WHERE code LIKE ? ORDER BY code DESC LIMIT 1",
            (prefix + "%",)).fetchone()
        seq = int(row[0].split("-")[-1]) + 1 if row else 1
        code = f"{prefix}{seq:03d}"
        conn.execute(
            "INSERT OR IGNORE INTO learnings(namespace, code, title, root_cause, lesson, "
            "fix, trigger_when, trigger_do, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (namespace, code, title, root_cause, lesson, fix, trigger_when, trigger_do,
             time.time()))
        conn.commit()
        return {"code": code, "title": title}
    finally:
        conn.close()


def list_learnings(namespace=None, limit=20, keyword=None):
    """查询教训（支持关键词过滤，用于检索注入）"""
    conn = db.get_conn()
    try:
        sql = "SELECT code, title, root_cause, lesson, fix, trigger_when, trigger_do FROM learnings"
        conds, params = [], []
        if namespace:
            conds.append("namespace=?"); params.append(namespace)
        if keyword:
            conds.append("(title LIKE ? OR lesson LIKE ? OR trigger_when LIKE ?)")
            params += [f"%{keyword}%"] * 3
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ===== curated 同步 =====

def sync_curated(namespace=None, top_k=10):
    """核心内容归集：从 extracts（高重要性）+ core_memories 生成 curated 摘要

    对标 auto_sync_curated：把核心事实/决策归集到 curated 表，供快速引用。
    2026-08-10：namespace=None → 全库（数据分布在 dialogue/core 等）
    """
    conn = db.get_conn()
    try:
        # 高重要性提取物（全库）
        if namespace:
            rows = conn.execute(
                "SELECT type, text, short, importance FROM extracts WHERE namespace=? "
                "AND importance>=4 ORDER BY importance DESC, id DESC LIMIT ?",
                (namespace, top_k)).fetchall()
        else:
            rows = conn.execute(
                "SELECT type, text, short, importance FROM extracts "
                "WHERE importance>=4 ORDER BY importance DESC, id DESC LIMIT ?",
                (top_k,)).fetchall()
        # 核心关键词（全库）
        if namespace:
            kws = conn.execute(
                "SELECT keyword, freq FROM core_memories WHERE namespace=? "
                "ORDER BY freq DESC LIMIT 10", (namespace,)).fetchall()
        else:
            kws = conn.execute(
                "SELECT keyword, freq FROM core_memories "
                "ORDER BY freq DESC LIMIT 10").fetchall()
    finally:
        conn.close()

    if not rows and not kws:
        return None

    parts = [f"- [{r['type']}] {r['short'] or r['text'][:100]} (重要性{r['importance']})"
             for r in rows]
    parts += [f"- 核心关键词: {r['keyword']}（{r['freq']}次）" for r in kws]
    content = "\n".join(parts)

    conn = db.get_conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO curated(namespace, topic, content, updated_at) "
            "VALUES(?,?,?,?)",
            (namespace, "core_summary", content, time.time()))
        conn.commit()
        return {"topic": "core_summary", "items": len(rows) + len(kws)}
    finally:
        conn.close()


# ===== 反思 =====

REFLECTION_PROMPT = """你是反思专家。基于近期记忆，写一篇简短反思（300字内），关注：
- 近期的工作模式与效率
- 重复出现的问题或教训
- 值得坚持的好习惯
- 下一步改进建议

保持具体、有证据支撑，不要空话套话。

近期记忆：
{text}
"""


def run_reflection(extractor, namespace=None, window_h=72):
    """每日反思：读近期记忆（全库）→ LLM 反思 → reflections 表（同日幂等）

    2026-08-10 修复：默认全库（数据分布在 core/dialogue 等 namespace，
    旧系统 diary/反思也是全局的；限定 default 导致空跑返回 None）
    """
    today = datetime.now().strftime("%Y-%m-%d")
    conn = db.get_conn()
    try:
        dup = conn.execute(
            "SELECT 1 FROM reflections WHERE namespace=? AND date=?",
            (namespace or "default", today)).fetchone()
        if dup:
            return {"status": "skip", "reason": "今日已反思"}
        since = time.time() - window_h * 3600
        if namespace:
            rows = conn.execute(
                "SELECT text FROM chunks WHERE namespace=? AND updated_at>? ORDER BY id DESC LIMIT 30",
                (namespace, since)).fetchall()
        else:
            rows = conn.execute(
                "SELECT text FROM chunks WHERE updated_at>? ORDER BY id DESC LIMIT 30",
                (since,)).fetchall()
    finally:
        conn.close()

    if not rows:
        return None

    text = "\n".join(r[0][:200] for r in rows)[:6000]
    from memory_server.pipeline import _call_llm
    content = _call_llm(extractor, REFLECTION_PROMPT.format(text=text))
    if not content:
        return None

    conn = db.get_conn()
    try:
        # N1 修复（2026-08-11 第二轮审计）：namespace=None → 空串，避免 NOT NULL
        # 约束冲突被 INSERT OR IGNORE 静默吞掉（此前反思从未真正落库，假跑）
        ns = namespace or ""
        conn.execute(
            "INSERT OR IGNORE INTO reflections(namespace, date, content, created_at) "
            "VALUES(?,?,?,?)",
            (ns, today, content, time.time()))
        conn.commit()
        return {"status": "ok", "date": today, "chars": len(content)}
    finally:
        conn.close()


# ===== 冲突解决 =====

def scan_conflicts(namespace=None):
    """KG 冲突扫描：同 (subject, predicate) 多个当前有效 object → 冲突报告

    对标 auto_resolve_conflicts：发现并标记冲突（KG 冲突修正已在 kg_add 自动做，
    这里扫描历史遗留 + 输出报告供人工复核）。
    """
    conn = db.get_conn()
    try:
        sql = ("SELECT subject, predicate, COUNT(DISTINCT object) n FROM triples "
               "WHERE valid_to IS NULL")
        params = []
        if namespace:
            sql += " AND namespace=?"
            params.append(namespace)
        sql += " GROUP BY subject, predicate HAVING n > 1"
        rows = conn.execute(sql, params).fetchall()
        conflicts = [{"subject": r[0], "predicate": r[1], "objects": r[2]} for r in rows]
        return {"conflicts": conflicts, "count": len(conflicts)}
    finally:
        conn.close()


# ===== 反馈信号 =====

def detect_feedback(namespace=None, window_h=24, state_file=None):
    """检测近期对话中的反馈信号：
      负反馈 → 写教训（LRN，对标 write_learnings）
      正反馈 → KG 强化（subject=触发主题, predicate=获得正反馈）

    审计🟡修复（2026-08-11 复审）：namespace 默认改 None 全库——原默认 'default'
    但 chunks 全在 dialogue/core 等 namespace → 轨道空转。
    幂等：同一天同信号不重复（基于信号文本 hash）。
    """
    state_file = state_file or os.path.join(
        os.path.expanduser("~"), ".memory-server", "feedback_state.json")
    state = {}
    if os.path.exists(state_file):
        try:
            with open(state_file) as f:
                state = json.load(f)
        except Exception:
            pass

    conn = db.get_conn()
    try:
        since = time.time() - window_h * 3600
        if namespace:
            rows = conn.execute(
                "SELECT text FROM chunks WHERE namespace=? AND updated_at>? ORDER BY id DESC LIMIT 100",
                (namespace, since)).fetchall()
        else:
            rows = conn.execute(
                "SELECT text FROM chunks WHERE updated_at>? ORDER BY id DESC LIMIT 100",
                (since,)).fetchall()
    finally:
        conn.close()

    today = datetime.now().strftime("%Y-%m-%d")
    seen = set(state.get(today, []))
    neg_found, pos_found = [], []

    # 审计 P0-2（2026-08-11 三审）：指令性用语误判负反馈（「不要夹带」「先不要动手」等），
    # 且 assistant 消息也被扫描。修复：① 只扫 user 消息（text 以 'user:' 开头，来自 ingest_jsonl 格式）
    # ② 剔除指令性短语（否定词+动作意图是任务指令，不是对 agent 表现的负反馈）
    INSTRUCTION_NOISE = (
        "不要夹带", "先不要", "不要直接", "别动手", "不要动手", "先别",
        "不用改", "不用做", "不需要", "不用管", "不要管", "不用再",
        "不要删", "先不动", "不急着", "别急", "不要急", "先放",
    )
    for (text,) in rows:
        text = text[:500]
        # 只扫 user 消息（agent_ingest 存为 'user: 内容' 或 json 的 role: user）
        low = text[:60].lower()
        if text.startswith("assistant:") or text.startswith("AI:") or \
                "\"role\": \"assistant\"" in text:
            continue
        if text.startswith("user:"):
            body = text[5:]
        elif "\"role\": \"user\"" in text:
            body = text
        else:
            body = text
        # 指令性用语（任务指令 ≠ 负反馈）直接跳过
        if any(kw in body for kw in INSTRUCTION_NOISE):
            continue
        for pattern, label in NEGATIVE_PATTERNS:
            m = re.search(pattern, body)
            if m:
                sig = f"neg:{label}:{text[:80]}"
                if sig not in seen:
                    seen.add(sig)
                    neg_found.append((label, body))
                break
        for pattern, label in POSITIVE_PATTERNS:
            if re.search(pattern, body):
                sig = f"pos:{label}:{text[:80]}"
                if sig not in seen:
                    seen.add(sig)
                    pos_found.append((label, body))
                break

    # 负反馈 → 教训
    written = []
    for label, text in neg_found[:3]:
        r = add_learning(namespace, f"[负反馈] {label}: {text[:60]}",
                         root_cause="用户反馈信号",
                         lesson=text[:200],
                         fix="结合上下文修正；同类问题避免重犯",
                         trigger_when=f"用户表达{label}",
                         trigger_do="回溯确认具体改进点并修正")
        written.append(r["code"])

    # 正反馈 → KG 强化
    kg_boosted = 0
    if pos_found:
        from memory_server.kg import kg_add
        for label, text in pos_found[:3]:
            # 从文本提取主题词（简单：取首个 2-4 字词元）
            m = re.search(r'[\u4e00-\u9fff]{2,4}', text)
            if m:
                r = kg_add(m.group(0), "获得正反馈", label, namespace=namespace)
                if r["status"] == "inserted":
                    kg_boosted += 1

    state[today] = sorted(seen)
    os.makedirs(os.path.dirname(state_file), exist_ok=True)
    with open(state_file, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    return {"negative": len(neg_found), "positive": len(pos_found),
            "learnings_written": written, "kg_boosted": kg_boosted}


def main():
    import argparse
    parser = argparse.ArgumentParser(prog="eidetic maintain", description="维护模块")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("conflicts").add_argument("--ns", default=None)
    p_fb = sub.add_parser("feedback")
    p_fb.add_argument("--ns", default="default")
    p_fb.add_argument("--window-h", type=int, default=24)
    sub.add_parser("curated").add_argument("--ns", default="default")
    sub.add_parser("reflect").add_argument("--ns", default="default")
    p_ls = sub.add_parser("learnings")
    p_ls.add_argument("--ns", default=None)
    p_ls.add_argument("--keyword", default=None)
    p_rb = sub.add_parser("reembed", help="补嵌空向量 chunk（N4：Ollama 偶发长度不匹配产生）")
    p_rb.add_argument("--limit", type=int, default=500)
    args = parser.parse_args()

    db.init_db()
    if args.cmd == "reembed":
        from memory_server.embed import detect_provider
        conn = db.get_conn()
        rows = conn.execute(
            "SELECT id, text FROM chunks WHERE embedding IS NULL LIMIT ?",
            (args.limit,)).fetchall()
        conn.close()
        if not rows:
            print("✅ 无空嵌入")
            return 0
        provider = detect_provider(prefer="ollama")
        texts = [r["text"][:2000] for r in rows]
        B = 32
        done = 0
        for i in range(0, len(texts), B):
            batch = texts[i:i + B]
            vecs = provider.embed(batch)
            conn = db.get_conn()
            for k, vec in enumerate(vecs):
                if vec:
                    conn.execute("UPDATE chunks SET embedding=? WHERE id=?",
                                 (db.blob_encode(vec), rows[i + k]["id"]))
            conn.commit()
            conn.close()
            done += len(batch)
        print(f"✅ 补嵌 {done} 条")
        return 0
    if args.cmd == "conflicts":
        r = scan_conflicts(namespace=args.ns)
        print(f"⚔️  冲突扫描: {r['count']} 处")
        for c in r["conflicts"]:
            print(f"  {c['subject']} {c['predicate']}: {c['objects']} 个当前值")
    elif args.cmd == "feedback":
        r = detect_feedback(namespace=args.ns, window_h=args.window_h)
        print(f"📡 反馈信号: 负 {r['negative']} / 正 {r['positive']} / "
              f"教训 {r['learnings_written']} / KG强化 {r['kg_boosted']}")
    elif args.cmd == "curated":
        r = sync_curated(namespace=args.ns)
        print(f"📌 curated 同步: {r}")
    elif args.cmd == "reflect":
        from memory_server.extract import detect_extractor
        r = run_reflection(detect_extractor(), namespace=args.ns)
        print(f"🪞 反思: {r}")
    elif args.cmd == "learnings":
        rs = list_learnings(namespace=args.ns, keyword=args.keyword)
        print(f"📚 教训 {len(rs)} 条:")
        for r in rs:
            print(f"  [{r['code']}] {r['title'][:60]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
