"""Y1 查询重写（2026-08-11 第二轮审计补全）：LLM 3 变体 + RRF 融合

背景：9号审计 Y1 —— 旧系统有 query 重写多变体能力，Eidetic 只做了同义词补丁。
本模块实现完整版：
  - LLM 变体：原查询 + 2 个改写（同义扩展 / 拆词补全），让模糊查询（如"产地"）变成
    可命中的精确变体（"原料产地 云南普洱 景迈山"）
  - 规则变体（LLM 不可用时降级）：KG 实体别名 + FTS 词元拆分
  - RRF 融合：每个变体独立召回 top-K，按 RRF 公式合并，避免单一变体压制

设计原则：
  - 默认关闭（--rewrite 显式开启），避免每次查询都烧 LLM
  - 无 LLM 时规则变体兜底，功能不中断
  - 与 fusion_search 解耦：本模块只产出"变体列表"，融合在 search.py 完成
"""
from __future__ import annotations

import os
import re
import time

# 同义/领域词表（轻量规则变体，不用 LLM 也能拆出可用变体）
_SYNONYMS = {
    "产地": ["原料产地", "来源地", "种植基地", "生产地"],
    "频率": ["更新频率", "调度间隔", "多久跑一次", "运行周期"],
    "定位": ["品牌定位", "目标人群", "市场定位"],
    "价格": ["价格区间", "多少钱", "售价"],
    "功能": ["功效", "作用", "效果"],
    "成分": ["原料", "配方", "本草"],
}


def _kg_entities(query: str) -> list[str]:
    """从 KG 实体表找与查询词相关的实体（别名/包含关系）"""
    import sqlite3
    from memory_server import db
    conn = db.get_conn()
    try:
        out = []
        for kw in re.split(r"[\s,，。]+", query.strip()):
            if len(kw) < 2:
                continue
            rows = conn.execute(
                "SELECT name, aliases FROM entities WHERE name LIKE ? OR aliases LIKE ? LIMIT 5",
                (f"%{kw}%", f"%{kw}%")).fetchall()
            for r in rows:
                # Y1 修复（2026-08-11）：实体名过长（>20 字符）说明是垃圾实体
                # （迁移时把整段文本当实体），不能用作变体——会污染 FTS/KG 召回
                if not r["name"] or len(r["name"]) > 20:
                    continue
                if r["name"] not in out:
                    out.append(r["name"])
                try:
                    import json
                    for a in json.loads(r["aliases"] or "[]"):
                        if a and len(a) <= 20 and a not in out:
                            out.append(a)
                except Exception:
                    pass
        return out[:8]
    finally:
        conn.close()


def _load_query_stopwords() -> set:
    """从 config.yaml 读 query_stopwords（私有词属于配置不属于代码——9号终审建议，
    2026-08-12：避免开源代码里再出现业务专属词；config 格式: query_stopwords: ["词1", "词2"]）"""
    extra = set()
    cfg_path = os.path.join(os.path.expanduser("~"), ".memory-server", "config.yaml")
    if not os.path.isfile(cfg_path):
        return extra
    try:
        with open(cfg_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("query_stopwords:"):
                    val = line.split(":", 1)[1].strip().strip("[]").strip()
                    if val:
                        extra = {x.strip().strip('"').strip("'") for x in val.split(",") if x.strip()}
    except Exception:
        pass
    return extra


# 模块级：默认停用词 + config 扩展（默认值仅通用词；业务词由部署者写入 config.yaml）
_STOP = {"source", "推理"} | _load_query_stopwords()


def _rule_variants(query: str) -> list[str]:
    """规则变体：同义词替换 + KG 实体补全（无 LLM 时兜底）"""
    q = query.strip()
    variants = [q]
    # 同义词扩展：把查询里的领域词替换成精确词
    for kw, repls in _SYNONYMS.items():
        if kw in q:
            for r in repls:
                v = q.replace(kw, r)
                if v != q and v not in variants:
                    variants.append(v)
            break
    # KG 实体补全：查询词 + 相关实体（如 "茶叶" → "茶叶 产地"）
    # Y1 修复：过滤泛词实体（source 等），它们不带来精确性；业务词由 config query_stopwords 扩展
    ents = [e for e in _kg_entities(q) if e not in _STOP and e not in q]
    if ents:
        for e in ents[:3]:
            v = f"{q} {e}"
            if v not in variants:
                variants.append(v)
    return variants[:4]


def _synonym_variants(query: str) -> list[str]:
    """仅同义词扩展变体（强信号）："产地" → "原料产地/来源地/..."

    2026-08-11：与 KG 实体补全变体区分——KG 补全（查询词+实体名，如
    "Active Memory 注入 active_recall"）是相关性提示，命中不该算强 boost，
    否则脚本名/配置名实体把大量历史开发对话顶上榜首（golden 30 条最后 1 条）。
    """
    q = query.strip()
    out = []
    for kw, repls in _SYNONYMS.items():
        if kw in q:
            for r in repls:
                v = q.replace(kw, r)
                if v != q and v not in out:
                    out.append(v)
            break
    return out


def _llm_variants(query: str, extractor=None) -> list[str]:
    """LLM 变体：让模型把模糊查询改写成 2 个可命中变体"""
    if extractor is None or extractor.name == "none":
        return []
    try:
        prompt = (
            "你是记忆检索查询改写器。把用户查询改写成 2 个更可能命中记忆库的变体：\n"
            "1) 同义扩展：补充领域术语（如 '产地'→'原料产地 云南普洱 景迈山'）\n"
            "2) 拆词补全：把泛词拆成具体可检索词\n"
            "只输出两行变体，不要编号、不要解释。\n"
            f"用户查询：{query}"
        )
        res = extractor.extract(prompt)
        if not res:
            return []
        text = res if isinstance(res, str) else str(res)
        lines = [l.strip() for l in text.splitlines() if l.strip()][:2]
        out = [l.lstrip("0123456789.、)） ").strip() for l in lines]
        return [v for v in out if v and v != query][:2]
    except Exception:
        return []


def rewrite(query: str, extractor=None, use_llm: bool = True) -> list[str]:
    """产出查询变体列表（含原查询），最多 3 个

    Args:
        query: 原始查询
        extractor: LLM 提炼器（可选，无则规则兜底）
        use_llm: 是否尝试 LLM 变体（默认 True；高频查询可关）

    Returns:
        ["原查询", "变体1", "变体2"]（去重，至少含原查询）
    """
    variants = [query.strip()] if query.strip() else []
    if use_llm:
        llm_v = _llm_variants(query, extractor)
        variants.extend(llm_v)
    rule_v = _rule_variants(query)
    for v in rule_v:
        if v not in variants:
            variants.append(v)
    return variants[:3]


def rrf_merge(ranked_lists: list[list[int]], k: int = 60) -> list[tuple[int, float]]:
    """RRF 融合：多路排名列表 → 合并分数（对齐旧系统 RRF 公式）"""
    scores: dict[int, float] = {}
    for lst in ranked_lists:
        for rank, cid in enumerate(lst):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)
