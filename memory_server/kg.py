#!/usr/bin/env python3
"""
memory-server 知识图谱（KG）模块（P1 收尾 2026-08-09）
========================================================
实体 + 三元组存储，带时间线（valid_from/valid_to）与冲突修正通道。
设计对齐 MemPalace KG：同 subject+predicate 出现新 object → 旧事实自动标记 valid_to（过时）。

接口：
  kg_add(subject, predicate, object, namespace, valid_from, valid_to, source_id)
  kg_query(entity, namespace, direction, as_of)
  kg_stats(namespace)
  kg_timeline(entity, namespace)
"""
import time
import json
import logging
from datetime import date

from . import db

log = logging.getLogger("memory-server.kg")


MAX_ENTITY_LEN = 64  # 审计🔴3（2026-08-11）：超过此长度的文本不当实体注册（防污染）

# 审计三审 P2-13（2026-08-11）：谓词规范化映射——中英混用统一为中文、
# 语义重复合并（is_part_of/part_of/has_part→属于；contains/includes→包含）。
# kg_add 写入时自动映射，存量已一次性 UPDATE。
PREDICATE_MAP = {
    "uses": "使用", "is_part_of": "属于", "part_of": "属于", "has_part": "属于",
    "contains": "包含", "includes": "包含", "says": "说", "creates": "创建",
    "improves": "改进", "competitor_of": "竞争", "mentions": "提及",
    "has_version": "版本", "provides": "提供", "has": "拥有",
}


def _ensure_entity(conn, namespace, name, etype="entity"):
    """注册实体（带归一化）：同名跳过；包含/被包含关系自动合并到规范名"""
    name = name.strip()
    if not name:
        return name
    canonical = normalize_entity(conn, namespace, name)
    # 规范名实体必须存在
    conn.execute(
        "INSERT OR IGNORE INTO entities(namespace, name, canonical, aliases, type, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (namespace, canonical, canonical, "[]", etype, time.time()),
    )
    # 若原始名不同于规范名，注册为别名实体（指向规范名）
    if name != canonical:
        conn.execute(
            "INSERT OR IGNORE INTO entities(namespace, name, canonical, aliases, type, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (namespace, name, canonical, json.dumps([canonical], ensure_ascii=False), etype, time.time()),
        )
    return canonical


def normalize_entity(conn, namespace, name):
    """实体归一化（Mem0 entity linking 借鉴，①）：
    查找已有实体的规范名——包含关系（A 是 B 的子串且长度≥2）→ 归入较长者。

    审计🔵修复（2026-08-11）：子串归并加长度差阈值（≥2）——
    原「A∈B 即并入」会把「白茶」并入「白茶树」这类误并（仅差 1 字的词根关系≠同实体）。
    别名要求明显更短（≥2 字差）才算缩写，如「茶叶」→「茶叶礼盒」（2→4 字）。

    示例: 已有「茶叶礼盒」时，新名「茶叶」→ 归入「茶叶礼盒」。
    """
    if len(name) < 2:
        return name
    # 1. 精确匹配已注册实体 → 直接用其 canonical
    row = conn.execute(
        "SELECT canonical FROM entities WHERE namespace=? AND name=? LIMIT 1",
        (namespace, name)).fetchone()
    if row:
        return row[0]
    # 2. 已注册实体名包含新名（新名是短别名，长度差≥2 才算缩写）→ 归入已注册实体
    # 审计六审 P1（2026-08-11）：原 SELECT DISTINCT canonical 全表扫（5-10 万实体时
    # kg_fast 秒级→分钟级）。优化：只拉长度 ≥ len(name)+2 的候选（能包含新名的长实体），
    # 大幅缩小扫描集；全表扫作为兜底（极小概率长实体未命中）。
    min_len = len(name) + 2
    rows = conn.execute(
        "SELECT DISTINCT canonical FROM entities WHERE namespace=? AND canonical!=? "
        "AND LENGTH(canonical) >= ? LIMIT 500",
        (namespace, name, min_len)).fetchall()
    for (canonical,) in rows:
        if canonical and name in canonical:
            return canonical
    # 2b. 兜底：候选不足 500 时可能存在更长实体，全量精确扫一次（低频路径）
    if len(rows) >= 500:
        rows2 = conn.execute(
            "SELECT DISTINCT canonical FROM entities WHERE namespace=? AND canonical!=? ",
            (namespace, name)).fetchall()
        for (canonical,) in rows2:
            if canonical and len(canonical) >= min_len and name in canonical:
                return canonical
    # 3. 新名包含已注册实体名（新名更长且长≥2 字，可能是完整名）→ 归入新名，旧的更新 canonical
    # 审计六审 P1：只拉长度 ≤ len(name)-2 的候选（能被新名包含的短实体）
    max_len = len(name) - 2
    rows = conn.execute(
        "SELECT DISTINCT canonical FROM entities WHERE namespace=? AND canonical!=? "
        "AND LENGTH(canonical) <= ? LIMIT 500",
        (namespace, name, max_len)).fetchall()
    for (canonical,) in rows:
        if canonical and len(canonical) >= 2 and canonical in name:
            conn.execute(
                "UPDATE entities SET canonical=? WHERE namespace=? AND canonical=?",
                (name, namespace, canonical))
            # 同步更新 triples 里的旧名（数据一致性：实体合并必须连坐）
            conn.execute(
                "UPDATE triples SET subject=? WHERE namespace=? AND subject=?",
                (name, namespace, canonical))
            conn.execute(
                "UPDATE triples SET object=? WHERE namespace=? AND object=?",
                (name, namespace, canonical))
            return name
    return name


def kg_add(subject, predicate, object, namespace="default",
           valid_from=None, valid_to=None, source_id=None):
    """写入一条事实（三元组）。

    幂等 + 冲突修正：
      - 完全相同的 (subject, predicate, object) → 跳过（不重复）
      - 同 (subject, predicate) 不同 object → 旧记录 valid_to = valid_from(新事实起点) 前一天，
        表示旧事实已过时（时间线修正通道）
    """
    if not subject or not predicate or not object:
        return {"status": "skip", "reason": "空字段"}
    # 谓词规范化（审计三审 P2-13）：中英混用 → 中文统一，语义重复合并
    predicate = PREDICATE_MAP.get(predicate, predicate)
    # 审计四审安全🔵（2026-08-11）：谓词长度校验（subject/object 有 64 上限，predicate 补上）
    if len(predicate.strip()) > MAX_ENTITY_LEN:
        return {"status": "skip", "reason": f"谓词超长(>{MAX_ENTITY_LEN}字符)"}
    # 审计🔴3（2026-08-11）：超长文本不当实体注册（extract.py 曾把 200 字内容当客体→实体污染）
    if len(subject.strip()) > MAX_ENTITY_LEN or len(object.strip()) > MAX_ENTITY_LEN:
        return {"status": "skip", "reason": f"实体超长(>{MAX_ENTITY_LEN}字符)",
                "subject": subject[:20], "object": str(object)[:20]}
    if valid_from is None:
        valid_from = date.today().isoformat()

    conn = db.get_conn()
    try:
        subject = _ensure_entity(conn, namespace, subject)
        object = _ensure_entity(conn, namespace, object)

        # 冲突检测：同 subject+predicate 已有不同 object 且在有效期内
        existing = conn.execute(
            "SELECT id, object, valid_to FROM triples "
            "WHERE namespace=? AND subject=? AND predicate=? AND object!=? AND valid_to IS NULL",
            (namespace, subject, predicate, object),
        ).fetchall()
        if existing:
            for eid, old_obj, _ in existing:
                log.info(f"KG 冲突修正: {subject} {predicate} {old_obj} → 标记过时")
                conn.execute(
                    "UPDATE triples SET valid_to=? WHERE id=?",
                    (valid_from, eid),
                )

        # 幂等插入
        cur = conn.execute(
            "INSERT OR IGNORE INTO triples(namespace, subject, predicate, object, "
            "valid_from, valid_to, source_id, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (namespace, subject, predicate, object, valid_from, valid_to,
             source_id, time.time()),
        )
        conn.commit()
        status = "inserted" if cur.rowcount else "duplicate"
        return {"status": status, "subject": subject, "predicate": predicate,
                "object": object, "valid_from": valid_from}
    finally:
        conn.close()


def kg_query(entity, namespace=None, direction="both", as_of=None):
    """查询实体的关系。

    direction: outgoing(entity→?) | incoming(?→entity) | both
    as_of: YYYY-MM-DD，只返回该时间点有效的事实
    返回: {"entity": e, "relations": [{predicate, object, direction, valid_from, valid_to}], "count": n}
    """
    conn = db.get_conn()
    try:
        # 查询归一化：别名 → 规范名（同实体不同名也能命中）
        q_entity = entity
        row = conn.execute(
            "SELECT canonical FROM entities WHERE namespace=? AND name=? LIMIT 1",
            (namespace or "default", entity)).fetchone()
        if row:
            q_entity = row[0]
        where_ns = "AND namespace=?" if namespace else ""
        params_ns = [namespace] if namespace else []
        relations = []

        if direction in ("outgoing", "both"):
            sql = (f"SELECT subject, predicate, object, valid_from, valid_to FROM triples "
                   f"WHERE subject=? {where_ns}")
            if as_of:
                sql += " AND (valid_from IS NULL OR valid_from<=?) AND (valid_to IS NULL OR valid_to>=?)"
                rows = conn.execute(sql, [q_entity] + params_ns + [as_of, as_of]).fetchall()
            else:
                rows = conn.execute(sql, [q_entity] + params_ns).fetchall()
            for s, p, o, vf, vt in rows:
                relations.append({"predicate": p, "object": o, "direction": "outgoing",
                                  "valid_from": vf, "valid_to": vt})

        if direction in ("incoming", "both"):
            sql = (f"SELECT subject, predicate, object, valid_from, valid_to FROM triples "
                   f"WHERE object=? {where_ns}")
            if as_of:
                sql += " AND (valid_from IS NULL OR valid_from<=?) AND (valid_to IS NULL OR valid_to>=?)"
                rows = conn.execute(sql, [q_entity] + params_ns + [as_of, as_of]).fetchall()
            else:
                rows = conn.execute(sql, [q_entity] + params_ns).fetchall()
            for s, p, o, vf, vt in rows:
                relations.append({"predicate": p, "object": s, "direction": "incoming",
                                  "valid_from": vf, "valid_to": vt})

        # 实体类型
        etype = None
        row = conn.execute("SELECT type FROM entities WHERE namespace=? AND name=?",
                           (namespace or "default", entity)).fetchone() if namespace else \
              conn.execute("SELECT type FROM entities WHERE name=? LIMIT 1", (entity,)).fetchone()
        if row:
            etype = row[0]

        return {"entity": entity, "canonical": q_entity if q_entity != entity else None,
                "type": etype, "relations": relations, "count": len(relations)}
    finally:
        conn.close()


def kg_invalidate(subject, predicate, object=None, namespace="default", valid_to=None):
    """显式将事实标记为过时（时间线修正通道）。

    与 KG 失效语义对齐：
      事实变化时先失效旧事实，再 kg_add 新事实。
    - 传 object → 只失效 (subject, predicate, object) 这条精确事实
    - 不传 object → 失效 (subject, predicate) 全部有效事实
    返回: {"status": "invalidated"|"noop", "count": n}
    """
    if not subject or not predicate:
        return {"status": "error", "reason": "subject/predicate 必填"}
    if valid_to is None:
        valid_to = date.today().isoformat()
    conn = db.get_conn()
    try:
        # 审计🔵修复（2026-08-11）：别名归一化——与 kg_add 一致，避免别名调用静默 noop
        canon_s = _canonical_of(conn, namespace, subject)
        if canon_s:
            subject = canon_s
        if object:
            canon_o = _canonical_of(conn, namespace, object)
            if canon_o:
                object = canon_o
        if object:
            cur = conn.execute(
                "UPDATE triples SET valid_to=? "
                "WHERE namespace=? AND subject=? AND predicate=? AND object=? AND valid_to IS NULL",
                (valid_to, namespace, subject, predicate, object),
            )
        else:
            cur = conn.execute(
                "UPDATE triples SET valid_to=? "
                "WHERE namespace=? AND subject=? AND predicate=? AND valid_to IS NULL",
                (valid_to, namespace, subject, predicate),
            )
        conn.commit()
        n = cur.rowcount
        return {"status": "invalidated" if n else "noop", "count": n,
                "valid_to": valid_to}
    finally:
        conn.close()


def _canonical_of(conn, namespace, name):
    """查询实体的规范名（别名→规范名，审计🔵归一化用）"""
    if not name:
        return None
    row = conn.execute(
        "SELECT canonical FROM entities WHERE namespace=? AND name=? LIMIT 1",
        (namespace, name)).fetchone()
    return row["canonical"] if row else None


def kg_stats(namespace=None):
    """KG 统计：实体数 / 三元组数 / predicate 分布 / 过时事实数"""
    conn = db.get_conn()
    try:
        where_ns = "WHERE namespace=?" if namespace else ""
        params = [namespace] if namespace else []
        entities = conn.execute(f"SELECT COUNT(*) FROM entities {where_ns}", params).fetchone()[0]
        triples = conn.execute(f"SELECT COUNT(*) FROM triples {where_ns}", params).fetchone()[0]
        expired_where = (where_ns + " AND") if namespace else "WHERE"
        expired = conn.execute(f"SELECT COUNT(*) FROM triples {expired_where} valid_to IS NOT NULL",
                               params).fetchone()[0]
        preds = conn.execute(
            f"SELECT predicate, COUNT(*) as n FROM triples {where_ns} GROUP BY predicate "
            f"ORDER BY n DESC LIMIT 10", params).fetchall()
        return {"entities": entities, "triples": triples, "expired": expired,
                "predicates": [{"predicate": p, "count": n} for p, n in preds]}
    finally:
        conn.close()


def kg_timeline(entity, namespace=None):
    """实体的时间线：按 valid_from 排序的全部事实"""
    conn = db.get_conn()
    try:
        if namespace:
            rows = conn.execute(
                "SELECT subject, predicate, object, valid_from, valid_to, created_at FROM triples "
                "WHERE (subject=? OR object=?) AND namespace=? ORDER BY valid_from",
                (entity, entity, namespace),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT subject, predicate, object, valid_from, valid_to, created_at FROM triples "
                "WHERE (subject=? OR object=?) ORDER BY valid_from",
                (entity, entity),
            ).fetchall()
        return [{"subject": s, "predicate": p, "object": o, "valid_from": vf,
                 "valid_to": vt, "created_at": ca} for s, p, o, vf, vt, ca in rows]
    finally:
        conn.close()


def entity_summary(extractor, namespace=None, top_n=10, dry_run=False):
    """③b 实体摘要（Graphiti 借鉴）：对高频实体生成综合状态摘要存 entities.summary

    数据：该实体的三元组 + 相关提取物 → LLM 综合（无 LLM 时规则拼接）。
    返回: {"summarized": n, "skipped": n}
    """
    conn = db.get_conn()
    try:
        # top 实体（按关联三元组数）
        sql = ("SELECT e.name, e.canonical, e.summary, "
               "(SELECT COUNT(*) FROM triples t WHERE t.namespace=e.namespace "
               "AND (t.subject=e.canonical OR t.object=e.canonical)) AS rel_n "
               "FROM entities e")
        params = []
        if namespace:
            sql += " WHERE e.namespace=?"
            params.append(namespace)
        sql += " ORDER BY rel_n DESC LIMIT ?"
        params.append(top_n * 2)
        rows = conn.execute(sql, params).fetchall()
        # 过滤无关联实体
        rows = [r for r in rows if r["rel_n"] > 0][:top_n]
    finally:
        conn.close()

    summarized = 0
    for r in rows:
        entity = r["canonical"]
        # 收集相关事实
        conn = db.get_conn()
        try:
            trips = conn.execute(
                "SELECT subject, predicate, object FROM triples WHERE namespace=? "
                "AND (subject=? OR object=?) AND valid_to IS NULL LIMIT 15",
                (namespace or "default", entity, entity)).fetchall()
            exts = conn.execute(
                "SELECT text FROM extracts WHERE namespace=? AND entities LIKE ? "
                "ORDER BY importance DESC LIMIT 8",
                (namespace or "default", f"%{entity}%")).fetchall()
        finally:
            conn.close()

        facts = [f"{t['subject']} {t['predicate']} {t['object']}" for t in trips]
        facts += [e["text"][:150] for e in exts]
        if not facts:
            continue
        prompt = ("基于以下事实，为实体「%s」生成一段综合摘要（100字内，描述当前状态）：\n\n%s"
                  % (entity, "\n".join(facts[:15])))
        summary = None
        if extractor and extractor.name != "none":
            from memory_server.pipeline import _call_llm
            summary = _call_llm(extractor, prompt)
        if not summary:
            # 规则兜底：拼接核心事实
            summary = "、".join(facts[:5])[:150]
        if dry_run:
            summarized += 1
            continue
        conn = db.get_conn()
        try:
            conn.execute(
                "UPDATE entities SET summary=? WHERE namespace=? AND canonical=?",
                (summary, namespace or "default", entity))
            conn.commit()
        finally:
            conn.close()
        summarized += 1
    return {"summarized": summarized, "scanned": len(rows)}


def main_summary(argv=None):
    import argparse
    parser = argparse.ArgumentParser(prog="eidetic entity-summary",
                                     description="实体摘要（Graphiti 借鉴）")
    parser.add_argument("--ns", default=None)
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    from memory_server.extract import detect_extractor
    r = entity_summary(detect_extractor(), namespace=args.ns, top_n=args.top,
                       dry_run=args.dry_run)
    print(f"🧠 实体摘要: 扫描 {r['scanned']} / 生成 {r['summarized']}"
          + ("（dry-run）" if args.dry_run else ""))
    return 0
