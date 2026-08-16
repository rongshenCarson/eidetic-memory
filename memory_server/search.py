#!/usr/bin/env python3
"""
memory-server 检索引擎（P1a）
=============================
混合检索：向量相似 + FTS5 trigram + 时间衰减 + namespace 过滤
"""
import os
import time
import json
import sqlite3
import logging

from . import db

log = logging.getLogger("memory-server.search")


def cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def _query_chunks(conn, namespace=None, include_archived=False, room=None,
                  with_embedding=True):
    """公共候选集查询（审计三审 P2-9 + 四审性能优化，2026-08-11）：
    base search 与 fusion 的 SQL 构造/过滤/执行曾大块重复（改一处漏一处），抽出统一。
    过滤：namespace / 沉底排除（json_extract）/ room（json_extract 结构化）
    with_embedding=False：不拉 embedding 列（4KB/行，94k 行 ≈ 380MB）——USearch 主路径用，
    numpy 回退时再按 id 补拉。
    """
    cols = "id, namespace, path, text, embedding, updated_at, metadata"
    if not with_embedding:
        cols = "id, namespace, path, text, updated_at, metadata"
    sql = f"SELECT {cols} FROM chunks"
    conds = []
    params = []
    if namespace:
        conds.append("namespace=?")
        params.append(namespace)
    if not include_archived:
        conds.append("json_extract(metadata, '$.archived') IS NOT 1")
    if room:
        conds.append("json_extract(metadata, '$.room.topic') = ?")
        params.append(room)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    return conn.execute(sql, params).fetchall()


def _fetch_embeddings(conn, ids):
    """按 id 补拉 embedding（审计四审性能：USearch 主路径不拉 embedding，回退 numpy 时才补）
    审计七审🟡：IN 分批 500/批——老 SQLite（Ubuntu 20.04 自带 3.31）变量上限 32766，
    94k+ 全量 IN 会报 too many SQL variables。"""
    if not ids:
        return {}
    result = {}
    B = 500
    for i in range(0, len(ids), B):
        batch = ids[i:i + B]
        ph = ",".join("?" * len(batch))
        rows = conn.execute(
            f"SELECT id, embedding FROM chunks WHERE id IN ({ph})", list(batch)).fetchall()
        result.update({r["id"]: r["embedding"] for r in rows})
    return result


def search(query, namespace=None, limit=5, vector_weight=0.6, provider=None, embed=True,
           include_archived=False, room=None):
    """混合检索：向量 + FTS + 时间衰减（默认排除沉底内容，深检索可 include_archived）"""
    conn = db.get_conn()
    try:
        # 1. FTS5 命中（trigram 中文子串）
        fts_ids = set()
        try:
            rows = conn.execute(
                "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? LIMIT 100",
                (query.replace('"', '""'),),
            ).fetchall()
            fts_ids = {r["rowid"] for r in rows}
        except Exception:
            pass

        # 2. 向量查询
        qvec = provider.embed([query])[0] if (provider and provider.dim and embed) else []

        # 3. 候选集（namespace 过滤 + 沉底过滤；全量打分——实测 94k 条无嵌入 0.31s，不做 LIMIT 截断，
        #    否则按 rowid 序只取最早插入的 core 数据，dialogue 等后导入的 namespace 被排除）
        # 审计四审性能优化：USearch 主路径不拉 embedding 列（94k 行省 ~380MB）
        rows = _query_chunks(conn, namespace, include_archived, room, with_embedding=False)

        # 向量化打分（2026-08-10 #42：numpy 矩阵一次算完，替代逐条 Python cosine）
        # 超量时自动切 USearch 索引（vector_index 模块，开源就绪）
        use_index = False
        index_pairs = []
        if qvec and rows and len(rows) >= 20000:
            from . import vector_index as vi
            vi._load_index()
            cand_ids = {r["id"] for r in rows}
            pairs = vi.search_topk(qvec, k=max(limit * 20, 200), include_ids=cand_ids)
            # R2 修复（2026-08-10）：pairs 空列表也要回退 numpy（小 namespace 在 top-k 候选里
            # 可能 0 命中——如小分区在大库中占比极低时，USearch 候选大概率为空）
            if pairs:
                use_index = True
                index_pairs = pairs

        if use_index:
            id_to_row = {r["id"]: r for r in rows}
            scored = []
            for cid, dist in index_pairs:
                r = id_to_row.get(cid)
                if not r:
                    continue
                cos = 1.0 - dist  # USearch cosine distance → 相似度
                fts_boost = 0.4 if r["id"] in fts_ids else 0.0
                age_days = (time.time() - r["updated_at"]) / 86400
                # Y5 统一：时间衰减改乘性（与 fusion 一致，避免新弱内容加性压过强向量）
                decay = 0.5 ** (age_days / 14)
                score = (cos * vector_weight + fts_boost) * (0.7 + 0.3 * decay)
                scored.append((score, r["id"], r["namespace"], r["text"]))
            scored.sort(reverse=True, key=lambda x: x[0])
            # R2 补充：结果不足 limit 时回退 numpy 补满（不 return，落到下方 numpy 分支）
            if len(scored) >= limit:
                return [
                    {"score": round(s, 4), "id": cid, "namespace": ns, "text": t[:200]}
                    for s, cid, ns, t in scored[:limit]
                ]
            # 不足 limit：把 USearch 结果并入下方 numpy 全量打分（不 return）
            _usearch_prefill = set(cid for _, cid, _, _ in scored)

        if qvec and rows:
            import numpy as np
            mats = []
            valid = []
            # 审计四审性能：rows 来自 with_embedding=False 查询，按 id 补拉 embedding
            emb_map = _fetch_embeddings(conn, [r["id"] for r in rows])
            for r in rows:
                vec = db.blob_decode(emb_map.get(r["id"]))
                if vec and len(vec) == len(qvec):
                    mats.append(vec)
                    valid.append(r)
            if mats:
                M = np.array(mats, dtype=np.float32)          # (N, dim)
                q = np.array(qvec, dtype=np.float32)          # (dim,)
                qn = np.linalg.norm(q)
                norms = np.linalg.norm(M, axis=1)
                denom = norms * qn
                denom[denom == 0] = 1e-9
                coss = (M @ q) / denom                          # (N,) 余弦
                rows = valid
                cos_list = coss.tolist()
            else:
                cos_list = [0.0] * len(rows)
        else:
            cos_list = [0.0] * len(rows)

        scored = []
        for idx, r in enumerate(rows):
            cos = cos_list[idx]
            fts_boost = 0.4 if r["id"] in fts_ids else 0.0
            age_days = (time.time() - r["updated_at"]) / 86400
            # Y5 统一：时间衰减改乘性（与 fusion 一致）
            decay = 0.5 ** (age_days / 14)  # 14 天半衰期
            score = (cos * vector_weight + fts_boost) * (0.7 + 0.3 * decay)
            scored.append((score, r["id"], r["namespace"], r["text"]))

        scored.sort(reverse=True, key=lambda x: x[0])
        return [
            {"score": round(s, 4), "id": cid, "namespace": ns, "text": t[:200]}
            for s, cid, ns, t in scored[:limit]
        ]
    finally:
        conn.close()


def _get_extractor():
    """Y1：取 LLM 提炼器（rewrite=True 时生成 LLM 变体）；无则 None → 规则变体兜底"""
    try:
        from memory_server.extract import detect_extractor
        return detect_extractor()
    except Exception:
        return None


def _kg_entity_expand(query, namespace, depth=1):
    """KG 实体扩展：查询中匹配的实体 → 多跳关联实体集合（F4 深度召回）

    返回: {"entities": [相关实体...], "chunk_ids": [相关 chunk id...]}
    """
    conn = db.get_conn()
    try:
        # 1. 从实体表找查询相关的实体（Y4：KG 属全局命名空间，全 ns 扫描——
        #    原实现绑 default，用户把 KG 写进业务 ns 就全断）
        q = query.strip()
        entities = []
        if q:
            rows = conn.execute(
                "SELECT name FROM entities WHERE name LIKE ? OR ? LIKE '%'||name||'%' "
                "LIMIT 10",
                (f"%{q}%", q),
            ).fetchall()
            entities = [r[0] for r in rows]
        if not entities:
            return {"entities": [], "chunk_ids": set()}

        # 2. 多跳扩展（全 ns）+ Y1 同义词扩展（2026-08-11：same_as/has_alias 边）
        expanded = set(entities)
        frontier = set(entities)
        # Y1：先取实体的直接同义词（same_as/has_alias/别名 三元组）加入扩展集
        try:
            syn_rows = conn.execute(
                "SELECT subject, object FROM triples "
                "WHERE predicate IN ('same_as','has_alias','alias','synonym','等价','别名') "
                "AND (subject IN (SELECT name FROM entities WHERE name LIKE ? OR ? LIKE '%'||name||'%') "
                "     OR object IN (SELECT name FROM entities WHERE name LIKE ? OR ? LIKE '%'||name||'%')) "
                "LIMIT 20",
                (f"%{q}%", q, f"%{q}%", q),
            ).fetchall()
            for r in syn_rows:
                if r["subject"]:
                    expanded.add(r["subject"])
                if r["object"]:
                    expanded.add(r["object"])
        except Exception:
            pass
        for _ in range(max(0, depth - 1)):
            nxt = set()
            for e in frontier:
                rows = conn.execute(
                    "SELECT subject, object FROM triples WHERE subject=? OR object=?",
                    (e, e),
                ).fetchall()
                for r in rows:
                    nxt.add(r[0]); nxt.add(r[1])
            new = nxt - expanded
            if not new:
                break
            expanded |= new
            frontier = new

        # 3. 关联 chunk：FTS 查每个实体名
        chunk_ids = set()
        for e in expanded:
            if not e:
                continue
            try:
                rows = conn.execute(
                    "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? LIMIT 20",
                    (e.replace('"', '""'),),
                ).fetchall()
                chunk_ids |= {r["rowid"] for r in rows}
            except Exception as e:
                log.debug(f"FTS 查询失败（实体 {e!r}）: {e}")
        return {"entities": list(expanded), "chunk_ids": chunk_ids}
    finally:
        conn.close()


def fusion_search(query, namespace=None, limit=5, provider=None, embed=True,
                  task_context=None, room=None, depth=1, include_archived=False,
                  vec_w=0.8, fts_w=0.1, kg_w=0.1, k=10, rewrite=False):
    """F4 融合检索（完整版）：向量 + FTS + KG 多跳 三路 RRF 融合
    + 时间衰减 + 任务偏置 + room 过滤

    参数:
      task_context: 任务上下文关键词（如项目名），命中记忆加权（对齐旧系统任务感知偏置）
      depth: KG 多跳深度（1=单跳，2=两跳）
      rewrite: 查询重写（Y1 完整版：LLM/规则多变体 + RRF，默认关闭防烧 LLM；
               规则变体零成本，LLM 变体仅在显式开启且 extractor 可用时启用）
    """
    conn = db.get_conn()
    try:
        # ---- Y1 查询重写（2026-08-11）：多变体召回 + RRF 融合 ----
        # 原查询 + 规则变体（KG 实体/同义词，零成本）始终参与；
        # LLM 变体仅 rewrite=True 时启用（防高频查询烧 token）
        from memory_server.rewrite import rewrite as _rewrite, rrf_merge, _synonym_variants
        variants = _rewrite(query, extractor=None if not rewrite else _get_extractor(),
                            use_llm=rewrite)
        # 2026-08-11：强信号只来自同义扩展变体（"产地"→"原料产地"）；
        # KG 实体补全变体（如 "Active Memory 注入 active_recall"）只是相关性提示，
        # 命中降为弱信号——否则脚本名/配置名实体把大量历史开发对话顶上榜首
        _syn_vs = set(_synonym_variants(query))
        # 各变体独立三路召回 → 合并候选集（RRF 融合在打分阶段处理）
        _v_fts = set()
        _v_fts_weak = set()
        _v_kg = set()
        for v in variants[1:]:  # 变体（不含原查询）额外补充 FTS/KG 命中
            try:
                rows_v = conn.execute(
                    "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? LIMIT 50",
                    (v.replace('"', '""'),)).fetchall()
                _hits = {r["rowid"] for r in rows_v}
                if v in _syn_vs:
                    _v_fts |= _hits
                else:
                    _v_fts_weak |= _hits
            except Exception:
                pass
            _v_kg |= set(_kg_entity_expand(v, namespace, depth=depth)["chunk_ids"])
        # 词元级 FTS 召回：把查询拆成 ≥3 字符词元（trigram 下限），OR 合并命中。
        # 整句 AND 对“产地 价格”这类多概念查询太严，拆词元才能让精确词命中（Y1 核心价值）
        import re as _re
        # 词元拆分修正（2026-08-11）：先按空白/标点切段，再提取每段的中文连续串（≥3字，
        # trigram 下限），英文/数字整段算一个词元。避免“5分钟”被数字截断丢“频率”。
        _tokens = []
        _sep = _re.compile(r"[\s,，。、？！;；:：()（）]+|(?<=[\u4e00-\u9fff])[A-Za-z0-9]+|(?<=[A-Za-z0-9])[\u4e00-\u9fff]+")
        for _seg in _sep.split(query):
            _seg = _seg.strip()
            if not _seg:
                continue
            if _re.fullmatch(r"[\u4e00-\u9fff]+", _seg):
                if len(_seg) >= 3:
                    _tokens.append(_seg)
            else:
                # 含数字/英文的段（如 5分钟、agent_ingest）：提取中文部分 + 整段
                _cn = _re.findall(r"[\u4e00-\u9fff]{3,}", _seg)
                _tokens.extend(_cn)
                if len(_seg) >= 3:
                    _tokens.append(_seg)
        # 词元 FTS 召回（拆词，放宽 AND）：原查询词元（如品牌词）命中是弱信号——
        # 泛词命中 7426 条里大量无关对话，不能算 strong。
        _tok_fts = set()
        for tok in _tokens:
            try:
                rows_t = conn.execute(
                    "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? LIMIT 300",
                    (tok.replace('"', '""'),)).fetchall()
                _tok_fts |= {r["rowid"] for r in rows_t}
            except Exception:
                pass
        # 原查询的 FTS/KG 命中（保持兼容）
        # ---- 三路召回 ----
        # 向量路（Y1：变体向量取最大相似度——原查询聚焦答案，变体拉远噪声，
        # 每个 chunk 取它在所有查询变体下的最高余弦，兼顾两全）
        qvec = provider.embed([query])[0] if (provider and provider.dim and embed) else []
        _qvecs = [qvec] if qvec else []
        if qvec and rewrite:
            for v in variants[1:]:
                try:
                    _qvecs.append(provider.embed([v])[0])
                except Exception:
                    pass
        # FTS 路
        fts_ids = set()
        try:
            rows = conn.execute(
                "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? LIMIT 100",
                (query.replace('"', '""'),),
            ).fetchall()
            fts_ids = {r["rowid"] for r in rows}
        except Exception:
            pass
        # Y1：变体词命中是强信号（如“原料产地”）；原查询词元/整句命中是弱信号
        fts_strong = set(_v_fts)
        fts_ids |= _v_fts | _v_fts_weak | _tok_fts
        # KG 路（实体多跳扩展）
        kg = _kg_entity_expand(query, namespace, depth=depth)
        kg_ids = kg["chunk_ids"] | _v_kg

        # ---- 候选集（审计 P2-9：统一走 _query_chunks；四审性能：不拉 embedding 列）----
        rows = _query_chunks(conn, namespace, include_archived, room, with_embedding=False)

        # ---- 两阶段融合（R1 修复 2026-08-10）----
        # 阶段1：候选集 = 向量 top-N ∪ FTS 命中 ∪ KG 命中
        # 阶段2：只在候选上做 FTS/KG/时间/任务偏置融合——性能 O(全库)→O(候选)，且不漏三路命中
        # 审计四审性能：向量 top-N 走 USearch（≥2 万条时），命中候选按 id 补拉 embedding 算精确余弦；
        # numpy 全量矩阵保留为回退（USearch 不可用/候选为空/小库）
        CANDIDATE_N = 300
        candidate = []
        seen = set()

        if qvec and rows:
            import numpy as np
            # USearch 快路径：先拿 top 候选 id（不拉全量 embedding）
            use_vi = False
            vi_pairs = []
            if len(rows) >= 20000:
                try:
                    from . import vector_index as vi
                    vi._load_index()
                    cand_ids = {r["id"] for r in rows}
                    vi_pairs = vi.search_topk(qvec, k=CANDIDATE_N * 10, include_ids=cand_ids)
                    if vi_pairs:
                        use_vi = True
                except Exception:
                    pass
            if use_vi:
                # 审计七审🔵：建 id→row 索引替代逐候选线性扫（600 候选 × 94k 行浪费）
                row_by_id = {r["id"]: r for r in rows}
                emb_map = _fetch_embeddings(conn, [cid for cid, _ in vi_pairs])
                for cid, dist in vi_pairs:
                    r = row_by_id.get(cid)
                    if not r:
                        continue
                    vec = db.blob_decode(emb_map.get(cid))
                    if vec and len(vec) == len(qvec):
                        candidate.append((r, 1.0 - dist))
                        seen.add(cid)
            # numpy 全量矩阵路径（USearch 不可用/候选不足时补满）
            # 审计八审🔵（2026-08-12）：use_vi=True 时也需 _fetch_embeddings 补拉
            # （原 emb_map=None 导致补满分支空转，是死逻辑）
            if not use_vi or len(candidate) < min(CANDIDATE_N, len(rows)):
                emb_map = _fetch_embeddings(conn, [r["id"] for r in rows])
                mats, valid = [], []
                for r in rows:
                    if r["id"] in seen:
                        continue
                    vec = db.blob_decode(emb_map.get(r["id"]))
                    if vec and len(vec) == len(qvec):
                        mats.append(vec)
                        valid.append(r)
                if mats:
                    M = np.array(mats, dtype=np.float32)
                    # Y1：多查询变体向量 → 每个 chunk 取最大余弦
                    qn_list = []
                    for qv in _qvecs:
                        qv = np.array(qv, dtype=np.float32)
                        qn_list.append(np.linalg.norm(qv))
                    norms = np.linalg.norm(M, axis=1)
                    best = None
                    for qv, qn in zip(_qvecs, qn_list):
                        qv = np.array(qv, dtype=np.float32)
                        denom = norms * qn
                        denom[denom == 0] = 1e-9
                        c = (M @ qv) / denom
                        best = c if best is None else np.maximum(best, c)
                    top_idx = np.argsort(-best)[:CANDIDATE_N]
                    for i in top_idx:
                        r = valid[i]
                        if r["id"] not in seen:
                            candidate.append((r, float(best[i])))
                            seen.add(r["id"])
        # 补 FTS/KG 命中（可能不在向量 top-N）
        for r in rows:
            if r["id"] in seen:
                continue
            if r["id"] in fts_ids or r["id"] in kg_ids:
                # 审计四审性能：rows 无 embedding 列，按 id 补拉
                vec = db.blob_decode(_fetch_embeddings(conn, [r["id"]]).get(r["id"]))
                cos = cosine(qvec, vec) if (qvec and vec and len(vec) == len(qvec)) else 0.0
                candidate.append((r, cos))
                seen.add(r["id"])
        # 兜底：候选太少时补前 N 条（保证有结果）
        if len(candidate) < min(limit, len(rows)):
            for r in rows:
                if r["id"] in seen:
                    continue
                candidate.append((r, 0.0))
                seen.add(r["id"])
                if len(candidate) >= max(limit, 20):
                    break

        k = 10
        scored = []
        # 审计🟡修复（2026-08-11）：FTS 权重可调——默认值与 golden 集调参一致，
        # 但开源用户可用环境变量调整（向量上限 ~0.086×0.8≈0.069 vs FTS 强命中 0.3，
        # FTS 权重约为向量上限的 4-5 倍；无语义词命中时语义排序会被泛词命中压制）。
        # 调小 FTS 权重可增强纯语义排序：MEMORY_FTS_STRONG=0.2 MEMORY_FTS_WEAK=0.1
        fts_strong_b = float(os.environ.get("MEMORY_FTS_STRONG", "0.3"))
        fts_weak_b = float(os.environ.get("MEMORY_FTS_WEAK", "0.2"))
        for r, cos in candidate:
            rrf_vec = cos / (cos + k) if cos > 0 else 0.0   # 0.94 → 0.086（强向量）
            # Y1（2026-08-11）：FTS 命中分层——强命中（变体扩展词，如“原料产地”）；
            # 弱命中（原查询词元/整句，如品牌词），避免泛词命中里无关对话霸榜。
            fts_boost = fts_strong_b if r["id"] in fts_strong else (fts_weak_b if r["id"] in fts_ids else 0.0)
            kg_boost = 0.05 if r["id"] in kg_ids else 0.0

            score = vec_w * rrf_vec + fts_boost + kg_boost

            # 时间衰减（14 天半衰期，乘性权重：新旧差异最多 ±30%，不颠覆排序）
            age_days = (time.time() - r["updated_at"]) / 86400
            decay = 0.5 ** (age_days / 14)
            score = score * (0.7 + 0.3 * decay)

            # 任务偏置（对齐旧系统 task-context ×2，适度加性 +0.05；G2：扩展到 path/metadata）
            if task_context:
                _ctx_haystack = r["text"] + " " + (r["path"] or "") + " " + (r["metadata"] or "")
                for kw in task_context.split():
                    if kw and kw in _ctx_haystack:
                        score += 0.05
                        break

            scored.append((score, r["id"], r["namespace"], r["text"]))

        scored.sort(reverse=True, key=lambda x: x[0])
        return {
            "results": [
                {"score": round(s, 4), "id": cid, "namespace": ns, "text": t[:200]}
                for s, cid, ns, t in scored[:limit]
            ],
            "kg_expanded": kg["entities"],
        }
    finally:
        conn.close()


def stats():
    conn = db.get_conn()
    try:
        total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        sources = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        by_ns = conn.execute("SELECT namespace, COUNT(*) c FROM chunks GROUP BY namespace").fetchall()
        return {
            "chunks": total,
            "sources": sources,
            "by_namespace": {r["namespace"]: r["c"] for r in by_ns},
        }
    finally:
        conn.close()


def expand_context(results, radius=1, max_chars=1600):
    """邻居扩展（对齐 MemPalace _expand_with_neighbors，2026-08-10 补）：
    命中 chunk 时返回同源文件 ±radius 的兄弟 chunk 组合，保证上下文完整不切断。

    实现：chunks.source_id 关联源文件，metadata.seq 是 chunk 顺序号。
    返回: 新 results（text 为扩展后的完整上下文，附 neighbors 信息）
    """
    conn = db.get_conn()
    try:
        expanded = []
        for r in results:
            row = conn.execute(
                "SELECT source_id, metadata FROM chunks WHERE id=?", (r["id"],)).fetchone()
            if not row or not row["source_id"]:
                expanded.append(r)
                continue
            sid = row["source_id"]
            seq = 0
            try:
                seq = int(json.loads(row["metadata"] or "{}").get("seq", 0))
            except Exception:
                pass
            sibs = conn.execute(
                "SELECT id, text, metadata FROM chunks WHERE source_id=? ORDER BY id",
                (sid,)).fetchall()
            if len(sibs) <= 1:
                expanded.append(r)
                continue
            # 找目标位置（按 id 序即 seq 序，同一文件连续插入）
            pos = next((i for i, s in enumerate(sibs) if s["id"] == r["id"]), 0)
            lo = max(0, pos - radius)
            hi = min(len(sibs), pos + radius + 1)
            parts = []
            for s in sibs[lo:hi]:
                parts.append(s["text"])
            full = "\n\n".join(parts)
            if len(full) > max_chars:
                full = full[:max_chars] + "...[截断]"
            nr = dict(r)
            nr["text"] = full
            nr["context"] = {"source_chunks": hi - lo, "pos": pos, "total": len(sibs)}
            expanded.append(nr)
        return expanded
    finally:
        conn.close()
