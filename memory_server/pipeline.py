#!/usr/bin/env python3
"""
memory-server 完整提炼管道（F2，完整版 2026-08-09）
======================================================
对标旧系统 memory_pipeline.py，适配单库架构：

  L1 结构化提炼   → extract.py（facts 四类 + episodics）✅ 已实现
  L2 场景归纳     → run_l2_scenes（24h 水位线）→ scenes 表
  L3 画像更新     → run_l3_persona（7d 水位线）→ persona 表
  核心记忆晋升    → run_promote（关键词频率 n-gram）→ core_memories 表

调度：service.default_tasks 接线（水位线补跑，崩溃/休眠自动恢复）
"""
import os
import re
import sys
import time
import json
import logging
from collections import Counter
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory_server import db  # noqa: E402

log = logging.getLogger("memory-server.pipeline")

SCENE_PROMPT = """你是场景归纳专家。根据今天的记忆，归纳当前项目的场景变化。

# 输出格式:
## 当前活跃场景
- **场景名**: 一句话描述当前状态（进行中/已完成/搁置）

## 场景变化
- **[新增]** 场景名: 描述
- **[推进]** 场景名: 从 X → Y
- **[关闭]** 场景名: 原因

## 关键进展
- 今天完成的重要决策或里程碑

只输出有实质变化的内容。如果今天没有变化，返回 "今日无场景变化"。

今日记忆：
{text}
"""

PERSONA_PROMPT = """你是用户画像维护专家。基于所有场景归纳，更新用户画像。

# 输出格式:
## 当前角色
- 核心身份与当前专注领域

## 长期偏好
- 不变的核心偏好

## 近期变化
- 最近一个月新增的兴趣、工具偏好、工作方式

## 关键决策风格
- 近期几个重大决策的模式分析

保持简洁，只记录有证据支撑的内容。不要臆测。

历史画像（如有）：
{persona}

场景归纳：
{scenes}
"""


def _call_llm(extractor, prompt):
    """调用 LLM 返回纯文本（非 JSON 格式的归纳/画像）"""
    if extractor is None or extractor.name == "none":
        return None
    try:
        if extractor.name == "deepseek":
            import urllib.request
            body = json.dumps({
                "model": extractor.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
            }).encode()
            req = urllib.request.Request(
                "https://api.deepseek.com/chat/completions",
                data=body,
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {extractor.api_key}"},
            )
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = json.loads(resp.read())
            return data["choices"][0]["message"]["content"]
        if extractor.name == "local":
            import urllib.request
            body = json.dumps({
                "model": extractor.model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "options": {"temperature": 0.3},
            }).encode()
            req = urllib.request.Request(
                f"{extractor.base_url}/api/chat",
                data=body,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read())
            return data["message"]["content"]
    except Exception as e:
        log.warning(f"LLM 调用失败: {e}")
    return None


def run_l2_scenes(extractor, namespace=None, window_h=24, date_str=None):
    """L2 场景归纳：读最近 window_h 的对话/提炼 → 归纳 → scenes 表（水位线在调度层）
    2026-08-10：namespace=None → 全库（数据分布在 dialogue/core 等）"""
    conn = db.get_conn()
    try:
        since = time.time() - window_h * 3600
        if namespace:
            rows = conn.execute(
                "SELECT text FROM chunks WHERE namespace=? AND updated_at>? ORDER BY id DESC LIMIT 50",
                (namespace, since),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT text FROM chunks WHERE updated_at>? ORDER BY id DESC LIMIT 50",
                (since,),
            ).fetchall()
        # 补充提取物
        if namespace:
            ext_rows = conn.execute(
                "SELECT text, short FROM extracts WHERE namespace=? AND created_at>? "
                "ORDER BY id DESC LIMIT 30", (namespace, since),)
        else:
            ext_rows = conn.execute(
                "SELECT text, short FROM extracts WHERE created_at>? "
                "ORDER BY id DESC LIMIT 30", (since,),)
        ext_rows = ext_rows.fetchall()
    finally:
        conn.close()

    if not rows and not ext_rows:
        return None

    parts = [r[0][:300] for r in rows[:30]]
    parts += [f"[提炼] {r[1] or r[0][:200]}" for r in ext_rows[:15]]
    text = "\n".join(parts)[:8000]
    content = _call_llm(extractor, SCENE_PROMPT.format(text=text))
    if not content:
        return None

    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    conn = db.get_conn()
    try:
        # Y5 修复（2026-08-11）：namespace=None（全库模式）→ 存空串，避免 NOT NULL 约束
        # 冲突被 INSERT OR IGNORE 静默吞掉（此前 L2 场景从未真正落库）
        ns = namespace or ""
        conn.execute(
            "INSERT OR IGNORE INTO scenes(namespace, date, title, content, status, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (ns, date_str, f"场景归纳 {date_str}", content, "active", time.time()),
        )
        conn.commit()
        log.info(f"L2 场景归纳完成 [{ns}] {date_str}")
        return {"date": date_str, "chars": len(content)}
    finally:
        conn.close()


def run_l3_persona(extractor, namespace=None, per_ns=False):
    """L3 画像更新：读场景归纳 + 历史画像 → 更新 persona 表
    2026-08-10 初：namespace=None → 全库
    2026-08-10 Y6：per_ns=True 时遍历有数据的 namespace 分别提炼（多租户防串扰）；
    单用户调度默认全库（与旧系统一致）"""
    if per_ns:
        # Y6：per-namespace 模式——遍历有场景数据的 ns 各自提炼（多租户安全）
        conn0 = db.get_conn()
        try:
            nss = [r[0] for r in conn0.execute(
                "SELECT DISTINCT namespace FROM scenes WHERE content IS NOT NULL").fetchall()]
        finally:
            conn0.close()
        results = []
        for ns in nss:
            r = _run_l3_one(extractor, ns)
            if r:
                results.append(r)
        return results or None
    return _run_l3_one(extractor, namespace)


def _run_l3_one(extractor, namespace=None):
    """L3 画像单 ns 提炼（Y6 拆分）"""
    conn = db.get_conn()
    try:
        if namespace:
            scenes = conn.execute(
                "SELECT date, content FROM scenes WHERE namespace=? ORDER BY date DESC LIMIT 30",
                (namespace,),
            ).fetchall()
        else:
            scenes = conn.execute(
                "SELECT date, content FROM scenes ORDER BY date DESC LIMIT 30").fetchall()
        if namespace:
            persona_row = conn.execute(
                "SELECT content FROM persona WHERE namespace=?", (namespace,)
            ).fetchone()
        else:
            persona_row = conn.execute(
                "SELECT content FROM persona ORDER BY updated_at DESC LIMIT 1").fetchone()
    finally:
        conn.close()

    if not scenes:
        return None

    scenes_text = "\n\n".join(f"[{d}] {c[:500]}" for d, c in scenes)[:8000]
    persona_old = persona_row[0] if persona_row else ""
    content = _call_llm(extractor, PERSONA_PROMPT.format(persona=persona_old, scenes=scenes_text))
    if not content:
        return None

    conn = db.get_conn()
    try:
        # Y5 修复（2026-08-11）：namespace=None → 空串，避免 PRIMARY KEY NOT NULL 冲突
        ns = namespace or ""
        conn.execute(
            "INSERT OR REPLACE INTO persona(namespace, content, updated_at) VALUES(?,?,?)",
            (ns, content, time.time()),
        )
        conn.commit()
        log.info(f"L3 画像更新完成 [{ns}]")
        return {"namespace": namespace, "chars": len(content)}
    finally:
        conn.close()


def run_promote(namespace=None, min_freq=5, top_n=20):
    """核心记忆晋升：n-gram 关键词频率 → 高频关键词入 core_memories（对标 promote_core_memories）
    2026-08-10：namespace=None → 全库
    2026-08-11 三审修复：全库时按每个 chunk 的真实 ns 写入（原 None → NOT NULL 冲突连续失败）
    """
    conn = db.get_conn()
    try:
        if namespace:
            rows = conn.execute(
                "SELECT text, namespace FROM chunks WHERE namespace=? ORDER BY id DESC LIMIT 500",
                (namespace,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT text, namespace FROM chunks ORDER BY id DESC LIMIT 500").fetchall()
    finally:
        conn.close()
    if not rows:
        return None

    keywords = Counter()
    ns_of = {}  # 关键词 → 来源 ns（取首个出现，统计时按 ns 分组）
    for text, ns in rows:
        for w in extract_ngrams(text):
            keywords[w] += 1
            ns_of.setdefault(w, ns or "default")
    # 保留 Counter 类型（most_common 方法），过滤低频
    promoted = Counter({k: v for k, v in keywords.items() if v >= min_freq})
    top = promoted.most_common(top_n)
    if not top:
        return None

    conn = db.get_conn()
    try:
        added = 0
        for kw, freq in top:
            # 审计🔵修复（2026-08-11 复审）：UPSERT——同关键词重复晋升时更新 freq（原 INSERT OR
            # IGNORE 直接跳过，freq 永不更新长期陈旧）；ns 用真实来源（NOT NULL 约束）
            cur = conn.execute(
                "INSERT INTO core_memories(namespace, keyword, freq, text, promoted_at) "
                "VALUES(?,?,?,?,?) "
                "ON CONFLICT(namespace, keyword) DO UPDATE SET freq=excluded.freq, "
                "text=excluded.text, promoted_at=excluded.promoted_at",
                (ns_of.get(kw, "default"), kw, freq,
                 f"高频关键词: {kw}（出现 {freq} 次）", time.time()),
            )
            added += cur.rowcount
        conn.commit()
        log.info(f"核心记忆晋升 [{namespace}]: {added} 关键词（≥{min_freq} 次）")
        return {"added": added, "top": [k for k, _ in top[:5]]}
    finally:
        conn.close()


# 审计🟡修复（2026-08-11）：停用词表——n-gram 晋升关键词里
# 「内容/文件/消息/现在/什么/所有/直接」等泛词无记忆价值（生产实证垃圾词）
STOPWORDS = {
    "内容", "文件", "消息", "现在", "什么", "所有", "直接", "可以", "这个", "那个",
    "我们", "你们", "他们", "自己", "时候", "知道", "觉得", "没有", "还是", "就是",
    "一个", "一种", "一下", "这样", "那样", "如果", "因为", "所以", "但是", "而且",
    "已经", "正在", "进行", "需要", "想要", "应该", "可能", "比较", "非常", "一直",
    "昨天", "今天", "明天", "之前", "之后", "同时", "还有", "以及", "对于", "关于",
    "问题", "事情", "东西", "结果", "开始", "继续", "看看", "好的", "收到", "了解",
}


def extract_ngrams(text, min_len=2, max_len=4):
    """提取中文 n-gram 关键词（对标 promote_core_memories.extract_ngrams）

    审计🟡修复（2026-08-11）：过滤停用词——原实现把「内容/文件/消息/现在」等
    高频泛词晋升为 core_memories，wake_up 高频主题输出质量差。
    """
    # 只取中文字符序列
    segments = re.findall(r'[\u4e00-\u9fff]{2,}', text)
    grams = []
    for seg in segments:
        n = len(seg)
        for size in range(min_len, min(max_len, n) + 1):
            for i in range(n - size + 1):
                g = seg[i:i + size]
                if g not in STOPWORDS:
                    grams.append(g)
    return grams
