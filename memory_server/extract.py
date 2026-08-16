#!/usr/bin/env python3
"""
memory-server 提炼插件（P1a）
==============================
L1-L3 + KG 提炼：LLM 抽取决策/事实/画像/三元组。
插件化设计（关键建议）：内核零 LLM 依赖，本插件可选启用。

Provider 可配：
  - deepseek（默认，需 DEEPSEEK_API_KEY）
  - local（本地 LLM，如 Ollama 的 qwen）
  - none（关闭提炼，内核不受影响）
"""
import os
import json
import time
import logging
from difflib import SequenceMatcher

from . import db

log = logging.getLogger("memory-server.extract")

EXTRACT_PROMPT = """你是记忆提取专家。从以下对话/文本中提取结构化记忆，输出 JSON。

# 类型一 facts(结构化事实,零散知识点):
1. decision (技术决策): 工具选型、架构选择、配置变更、方案抉择
2. preference (偏好设定): 模型偏好、风格偏好、工具偏好、品牌偏好
3. fact (事实信息): 品牌/业务事实、个人信息、项目状态、版本变更
4. milestone (里程碑): 重大项目完成、版本升级、流程变更

# 类型二 episodics(对话经历摘要,连贯成段的复盘笔记):
- scene: 一句话场景(如"AI与用户调优记忆系统召回质量")
- summary: 2-4句完整叙述,说清做了什么、遇到什么、结论是什么(能独立读懂,不依赖上下文)
- 要像"事后复盘笔记"连贯成段,不要零散点

# 规则:
- 每条附时间戳（从对话上下文推断,格式 YYYY-MM-DD）
- facts 识别关联实体（人物、项目、工具、品牌）
- 评估重要性 (1-5): 5=核心架构决策/重大进展, 3=常规操作, 1=一次性闲聊
- 只提取有价值的信息，跳过"让我看看""测试一下"等操作日志
- 没有可保留内容时对应数组返回空

# 输出格式:
{{"facts": [{{"type": "decision", "content": "...", "short": "1句话压缩,用于召回注入时节省token", "timestamp": "YYYY-MM-DD", "entities": ["实体1"], "importance": 4}}],
 "episodics": [{{"scene": "...", "summary": "...", "short": "1-2句话压缩版,独立可读,用于召回注入", "timestamp": "YYYY-MM-DD", "importance": 4}}]}}

# short 字段编写规则(对标mem0的短事实风格):
- facts.short: 1句话(<30字)说清决策/偏好/事实,去掉冗余修饰
- episodics.short: 1-2句话(<60字)压缩复盘,保留核心结论,去掉过程叙述
- embedding用完整版(content/summary), short仅在召回注入时用(省token)

文本：
{text}
"""


class BaseExtractor:
    def __init__(self):
        self.name = "base"

    def extract(self, text):
        raise NotImplementedError


def _get_deepseek_key():
    """从 OpenClaw auth 存储读取 DeepSeek key（环境变量 → agent sqlite → auth-profiles.json）"""
    import sqlite3 as _s2
    # 1. 环境变量
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if key:
        return key
    # 2. OpenClaw agent sqlite（v2026.6.8+ 存储方式，对齐旧 auto_extract_cron）
    for db_path in (
        os.path.expanduser("~/.openclaw/agents/main/agent/openclaw-agent.sqlite"),
        os.path.expanduser("~/.openclaw/auth.db"),
    ):
        try:
            if not os.path.exists(db_path):
                continue
            conn = _s2.connect(db_path)
            cur = conn.cursor()
            cur.execute('SELECT store_json FROM auth_profile_store WHERE store_key="primary"')
            row = cur.fetchone()
            conn.close()
            if row:
                data = json.loads(row[0])
                profiles = data.get("profiles", {})
                for pk in ("deepseek:default", "deepseek"):
                    ds = profiles.get(pk, {})
                    k = ds.get("key", "")
                    if k:
                        return k
        except Exception:
            continue
    # 3. auth-profiles.json
    try:
        with open(os.path.expanduser("~/.openclaw/agents/main/agent/auth-profiles.json")) as f:
            profiles = json.load(f).get("profiles", {})
            return profiles.get("deepseek:default", {}).get("key", "") or profiles.get("deepseek", {}).get("key", "")
    except Exception:
        pass
    return ""


class DeepSeekExtractor(BaseExtractor):
    """DeepSeek LLM 抽取（默认，key 从 OpenClaw auth 存储读取）"""

    def __init__(self, api_key=None, model="deepseek-chat"):
        super().__init__()
        self.name = "deepseek"
        self.api_key = api_key or _get_deepseek_key()
        self.model = model
        if not self.api_key:
            raise ValueError("DeepSeek key 未配置（环境变量 DEEPSEEK_API_KEY 或 OpenClaw auth 存储）")

    def extract(self, text):
        import urllib.request
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "user", "content": EXTRACT_PROMPT.format(text=text[:6000])}],
            "temperature": 0.2,
        }).encode()
        req = urllib.request.Request(
            "https://api.deepseek.com/chat/completions",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
            content = data["choices"][0]["message"]["content"]
            # 提取 JSON 部分
            start = content.find("{")
            end = content.rfind("}") + 1
            return json.loads(content[start:end])
        except Exception as e:
            log.warning(f"DeepSeek 抽取失败: {e}")
            return {"decisions": [], "facts": [], "terms": []}


class LocalExtractor(BaseExtractor):
    """本地 LLM 抽取（Ollama）"""

    def __init__(self, model="qwen2.5:7b", base_url="http://localhost:11434"):
        super().__init__()
        self.name = "local"
        self.model = model
        self.base_url = base_url

    def extract(self, text):
        import urllib.request
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "user", "content": EXTRACT_PROMPT.format(text=text[:6000])}],
            "stream": False,
            "options": {"temperature": 0.2},
        }).encode()
        req = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
            content = data["message"]["content"]
            start = content.find("{")
            end = content.rfind("}") + 1
            return json.loads(content[start:end])
        except Exception as e:
            log.warning(f"本地抽取失败: {e}")
            return {"decisions": [], "facts": [], "terms": []}


class NoneExtractor(BaseExtractor):
    """关闭提炼（内核不受影响）"""

    def __init__(self):
        super().__init__()
        self.name = "none"

    def extract(self, text):
        return {"decisions": [], "facts": [], "terms": []}


def detect_extractor(prefer=None):
    """自动探测提炼后端：deepseek(有key) > local(ollama 可用对话模型) > none"""
    if prefer == "none":
        return NoneExtractor()
    try:
        return DeepSeekExtractor()
    except ValueError:
        pass
    try:
        model = _detect_local_model()
        if model:
            return LocalExtractor(model=model)
    except Exception:
        pass
    log.warning("⚠️ 无 LLM 提炼后端（无 API key / 无本地模型）→ 提炼关闭，内核不受影响")
    return NoneExtractor()


def _detect_local_model():
    """探测 ollama 可用对话模型（排除嵌入/视觉模型）"""
    import urllib.request
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=3) as resp:
            data = json.loads(resp.read())
        models = [m["name"] for m in data.get("models", [])]
        # 偏好：qwen 系对话模型 > llama > 其他（排除 bge/embedding/vl 视觉）
        skip = ("bge", "embed", "vl", "vision", "nomic")
        candidates = [m for m in models if not any(s in m.lower() for s in skip)]
        if not candidates:
            return None
        for pref in ("qwen", "llama", "gemma"):
            for m in candidates:
                if m.lower().startswith(pref):
                    return m
        return candidates[0]
    except Exception:
        return None


def _is_near_duplicate(conn, namespace, text, ratio=0.88):
    """L1 更新决策 NOOP 检查：与最近提取物逐字相似度 > 阈值 → 视为重复不累积。

    注：KG 层冲突修正（同谓词新客体 → 旧 valid_to）负责「更新」决策；
    本函数负责「NOOP」决策（几乎相同的表述不重复入库）。
    """
    rows = conn.execute(
        "SELECT text FROM extracts WHERE namespace=? AND type!='aaak' "
        "ORDER BY id DESC LIMIT 50", (namespace,)).fetchall()
    for (existing,) in rows:
        if not existing:
            continue
        if SequenceMatcher(None, text[:200], existing[:200]).ratio() > ratio:
            return True
    return False


# ===== 确定性提取（规则轨道，#16，无 LLM 也能产出 KG） =====
DETERMINISTIC_PATTERNS = [
    # (pattern, predicate 模板)
    (r'([\u4e00-\u9fffA-Za-z]{2,20})(?:是|为)([\u4e00-\u9fffA-Za-z]{2,20})(?![\u4e00-\u9fffA-Za-z])', "{s} 是 {o}"),
    (r'([\u4e00-\u9fffA-Za-z]{2,20})(?:由)([\u4e00-\u9fffA-Za-z]{2,20})(?:生产|制造|提供|负责|出品)', "{s} 由 {o} 生产"),
    (r'([\u4e00-\u9fffA-Za-z]{2,20})(?:属于|隶属于)([\u4e00-\u9fffA-Za-z]{2,20})', "{s} 属于 {o}"),
    (r'([\u4e00-\u9fffA-Za-z]{2,20})(?:位于|地处|成立于)([\u4e00-\u9fffA-Za-z0-9]{2,20})', "{s} 位于 {o}"),
    (r'([\u4e00-\u9fffA-Za-z]{2,20})(?:采用|使用|基于)([\u4e00-\u9fffA-Za-z]{2,20})(?:方案|技术|架构|方法)?', "{s} 采用 {o}"),
]


# 审计八审🟡（2026-08-12）：主语停用词——口语「X是Y」正则必然误匹配
# （「倍慢 是 它自身问题」「这正 是 生命周期闭环该有的样子」），64 字上限拦不住短垃圾。
# 主语命中停用词/虚词 → 跳过（只产真实名词主语）
SUBJECT_STOPWORDS = {
    "这个", "那个", "这些", "那些", "这", "那", "可能", "应该", "可以", "确实",
    "其实", "就是", "不是", "还是", "都是", "正是", "本来", "基本", "真的",
    "主要", "特别", "尤其", "另外", "同时", "然后", "所以", "因为", "但是",
    "如果", "虽然", "而且", "并且", "以及", "还有", "没有", "不要", "不能",
    "我们", "你们", "他们", "咱们", "自己", "别人", "大家", "我", "你", "他",
    "倍", "慢", "快", "点", "话", "事", "东西", "情况", "问题", "时候",
}


def deterministic_extract(text):
    """规则确定性提取：实体 + 三元组（零 LLM 依赖，对标旧 auto_extract 确定性轨道）。

    审计八审🟡：主语停用词过滤（口语代词/虚词不落 KG）。
    返回: {"facts": [{"subject","predicate","object"}], "entities": [names]}
    """
    import re as _re
    facts, entities = [], []
    for pattern, tmpl in DETERMINISTIC_PATTERNS:
        for m in _re.finditer(pattern, text):
            s, o = m.group(1).strip(), m.group(2).strip()
            if len(s) < 2 or len(o) < 2:
                continue
            if s in SUBJECT_STOPWORDS or o in SUBJECT_STOPWORDS:
                continue
            facts.append({"subject": s, "predicate": _pred_from_tmpl(tmpl, s, o),
                          "object": o})
            entities.extend([s, o])
    return {"facts": facts, "entities": list(dict.fromkeys(entities))}


def _pred_from_tmpl(tmpl, s, o):
    """从模板生成谓词：'{s} 是 {o}' → '是'；'{s} 由 {o} 生产' → '由...生产'"""
    parts = tmpl.replace("{s}", "").replace("{o}", "").split()
    return "".join(parts) if parts else "相关"


def extract_and_store(text, namespace="default", extractor=None, source_id=None):
    """提取并入库（F2 完整版，对齐旧系统 memory_pipeline L1）：
      - facts（decision/preference/fact/milestone）→ extracts 表 + 实体注册 KG
      - episodics（scene/summary）→ extracts(type=episodic)
      - 保留 short/importance/entities/timestamp 字段

    返回: {"facts": n, "episodics": n, "kg": {...}}
    """
    if extractor is None or extractor.name == "none":
        return None

    result = extractor.extract(text)
    if not result:
        return None

    conn = db.get_conn()
    stats = {"facts": 0, "episodics": 0, "kg": None}
    try:
        now = time.time()
        # facts → extracts + KG 实体关联
        for f in result.get("facts", []):
            ftype = f.get("type", "fact")
            content = (f.get("content") or "").strip()
            if not content:
                continue
            # ②d L1 更新决策（对标旧 memory_pipeline）：语义近似 → NOOP 跳过，不无脑累积
            if _is_near_duplicate(conn, namespace, content):
                stats["facts"] += 1  # 计为已处理但未新增
                continue
            conn.execute(
                "INSERT OR IGNORE INTO extracts(namespace, type, text, short, importance, "
                "entities, tags, timestamp, source_id, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (namespace, ftype, content, (f.get("short") or "").strip()[:200],
                 int(f.get("importance") or 3),
                 json.dumps(f.get("entities", []), ensure_ascii=False),
                 json.dumps([], ensure_ascii=False),
                 f.get("timestamp"), source_id, now),
            )
            stats["facts"] += 1

        # episodics → extracts(type=episodic)
        for e in result.get("episodics", []):
            summary = (e.get("summary") or "").strip()
            if not summary:
                continue
            scene = (e.get("scene") or "").strip()
            conn.execute(
                "INSERT OR IGNORE INTO extracts(namespace, type, text, short, importance, "
                "entities, tags, timestamp, source_id, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (namespace, "episodic", summary, (e.get("short") or "").strip()[:200],
                 int(e.get("importance") or 3), "[]",
                 json.dumps(["scene"] if scene else [], ensure_ascii=False),
                 e.get("timestamp"), source_id, now),
            )
            stats["episodics"] += 1
        conn.commit()

        # facts 实体注册 KG（subject 维度：实体 → 关联内容摘要，防长文本污染实体表）
        # 审计🔴3修复（2026-08-11）：原 content[:200] 当客体 → 整段文本注册为实体（8k+ 垃圾实体）。
        # 客体用 short 摘要截断（≤64），超长内容不注册实体（kg_add 侧另有长度拦截双保险）。
        # 注：实体间共现关系暂未实现（注释对齐代码，2026-08-11 复审）
        kg_stats_result = {"added": 0, "duplicate": 0}
        from .kg import kg_add, MAX_ENTITY_LEN
        for f in result.get("facts", []):
            content = (f.get("content") or "").strip()
            if not content:
                continue
            ents = [e.strip() for e in (f.get("entities") or [])[:3] if e.strip()]
            if not ents:
                continue
            # 客体：用短摘要（长度受限），防长文本注册为实体
            short_obj = (f.get("short") or "").strip()[:MAX_ENTITY_LEN]
            for ent in ents:
                r = kg_add(ent, "涉及", short_obj, namespace=namespace, source_id=source_id)
                kg_stats_result[r.get("status", "inserted")] = \
                    kg_stats_result.get(r.get("status", "inserted"), 0) + 1
        stats["kg"] = kg_stats_result
        return stats
    finally:
        conn.close()
