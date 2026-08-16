#!/usr/bin/env python3
"""
memory-server MCP Server（P1a）
================================
MCP 协议接口：任何支持 MCP 的 Agent（OpenClaw/Claude Code/hermes 等）都能接入。

提供工具：
  - memory_search(query, namespace, limit)  检索记忆
  - memory_ingest(path, namespace)          摄入文件/目录
  - memory_status()                         状态

运行：python -m memory_server mcp
"""
import os
import sys
import re

from . import db
from .search import search, stats

# ---- 场景感知自动化（2026-08-11，混合型 ④）----
# ③ agent 显式传：task_context 参数（MCP 调用方主动声明）
# ② 会话级记忆：进程内记住最近一次 task_context，后续调用未传时复用
#    （MCP stdio 进程 = 一个 agent 会话，进程生命周期即会话生命周期）
# ① 保守兜底：无任何上下文时，查询词命中 KG 实体则自动提取实体名作偏置
#    （低置信度不启用：只有明确实体匹配才生效）
_session_task_context = None
_session_ctx_ts = 0.0
_SESSION_CTX_TTL = 3600  # 1 小时未活动则过期，避免跨任务串扰


def _resolve_task_context(explicit: str, query: str, conn) -> str:
    """场景感知上下文解析：③显式 > ②会话记忆 > ①KG实体兜底"""
    global _session_task_context, _session_ctx_ts
    import time as _t
    now = _t.time()
    if explicit is not None:
        # ③ 显式传：更新会话记忆（非空才记，空串=清除）
        if explicit.strip():
            _session_task_context = explicit.strip()
            _session_ctx_ts = now
        else:
            _session_task_context = None
        return _session_task_context or ""
    # ② 会话记忆：未显式传，但有会话级上下文且未过期
    if _session_task_context and (now - _session_ctx_ts) < _SESSION_CTX_TTL:
        return _session_task_context
    # ① 保守兜底：查询词命中 KG 实体（明确匹配才算，低置信度不启用）
    # 审计🟡修复（2026-08-11）：原正则 [\s,，。]+|[A-Za-z0-9]+ 把英文/数字当分隔符吃掉
    # → 英文实体名永不命中。改为：中文按标点/空白切，英文单词整体保留
    try:
        for kw in re.findall(r"[A-Za-z][A-Za-z0-9_-]*|[\u4e00-\u9fff]{2,}", query):
            kw = kw.strip()
            if len(kw) < 2:
                continue
            row = conn.execute(
                "SELECT name FROM entities WHERE name=? LIMIT 1", (kw,)).fetchone()
            if row and row["name"] not in ("source", "推理"):
                return row["name"]
    except Exception:
        pass
    return ""


def build_mcp_server(provider):
    """构建 MCP server（fastmcp）"""
    from fastmcp import FastMCP

    mcp = FastMCP("memory-server")
    # 审计🟡修复（2026-08-11）：原全局共享单条连接跨线程（fastmcp 线程池调用同步工具）
    # → sqlite3 ProgrammingError 风险。改为每次调用取新连接（与其余模块一致，finally 关闭）
    def _conn():
        return db.get_conn()

    @mcp.tool()
    def memory_search(query: str, namespace: str = None, limit: int = 5,
                      fusion: bool = False, task_context: str = None,
                      room: str = None, depth: int = 1, full: bool = False,
                      include_archived: bool = False) -> str:
        """检索记忆。query=查询词, namespace=分区(可选), limit=返回条数,
        fusion=是否融合检索(向量+FTS+KG多跳), task_context=任务上下文关键词
        （可选：传了就记住，后续同会话检索自动复用；不传则用会话级记忆或自动推断）,
        room=按主题过滤, depth=KG多跳深度, full=返回完整文本+邻居上下文(默认200字截断),
        include_archived=是否包含沉底内容(默认排除)"""
        # 审计🔵修复（2026-08-11 复审）：_resolve_task_context 的 conn 用后必须关闭，
        # 否则常驻 MCP 进程每次搜索泄漏一条连接
        _ctx_conn = _conn()
        try:
            _ctx = _resolve_task_context(task_context, query, _ctx_conn)
        finally:
            _ctx_conn.close()
        if fusion:
            from .search import fusion_search
            result = fusion_search(query, namespace=namespace, limit=limit, provider=provider,
                                   task_context=_ctx or None, room=room, depth=depth,
                                   include_archived=include_archived)
            results = result["results"]
        else:
            from .search import search
            results = search(query, namespace=namespace, limit=limit, provider=provider,
                             room=room, include_archived=include_archived)
        if not results:
            return "无相关记忆"
        if full:
            # 审计🔴修复（2026-08-11 复审）：expand_context 期望 dict 列表（含 id 键），
            # 返回 list（非 dict）——原实现传元组 + 当 dict 用 .get → 必崩（双重 bug）
            from .search import expand_context
            expanded_rows = expand_context(
                [{"id": r["id"], "score": r["score"], "namespace": r["namespace"],
                  "text": r["text"]} for r in results],
                radius=1, max_chars=2000)
            lines = [f"[{r['score']:.3f}] ({r['namespace']}) {r['text']}" for r in expanded_rows]
        else:
            lines = [f"[{r['score']:.3f}] ({r['namespace']}) {r['text']}" for r in results]
        # 审计四审安全🟡（2026-08-11）：记忆内容边界包裹——检索结果可能含恶意注入文本
        # （"忽略先前指令"类），用 <memory> 标签隔离，提示下游按数据对待
        return "<memory>\n" + "\n".join(lines) + "\n</memory>"

    @mcp.tool()
    def memory_set_context(task_context: str) -> str:
        """显式设置当前会话的任务上下文（场景感知）。传空串清除。
        设置后，同会话内后续 memory_search 未传 task_context 时自动复用。"""
        global _session_task_context, _session_ctx_ts
        import time as _t
        if task_context and task_context.strip():
            _session_task_context = task_context.strip()
            _session_ctx_ts = _t.time()
            return f"✅ 会话任务上下文已设置: {_session_task_context}"
        _session_task_context = None
        return "✅ 会话任务上下文已清除"

    @mcp.tool()
    def memory_ingest(path: str, namespace: str = "default") -> str:
        """摄入文件或目录到记忆库。path=文件/目录路径, namespace=分区"""
        # 审计四审安全🟡（2026-08-11）：任意路径读防护——默认拒绝主目录外的绝对路径，
        # 可用 MEMORY_INGEST_ALLOW 配置允许前缀（防 prompt injection 诱导摄入 /etc/passwd 等）
        expanded = os.path.expanduser(path)
        allow = os.environ.get("MEMORY_INGEST_ALLOW", "")
        if os.path.isabs(expanded):
            home = os.path.expanduser("~")
            allowed = [home, os.path.abspath(".")]
            if allow:
                allowed += [os.path.abspath(p) for p in allow.split(os.pathsep)]
            if not any(expanded == a or expanded.startswith(a + os.sep) for a in allowed):
                return f"❌ 拒绝摄入路径（不在允许范围）: {path}；可用 MEMORY_INGEST_ALLOW 配置"
        from .ingest import ingest_dir, ingest_file, ingest_jsonl
        if os.path.isdir(path):
            results = ingest_dir(path, namespace, provider)
        elif path.endswith(".jsonl"):
            results = [ingest_jsonl(path, namespace, provider)]
        else:
            results = [ingest_file(path, namespace, provider)]
        ok = sum(1 for r in results if r["status"] == "ok")
        return f"摄入完成: {ok}/{len(results)} 成功"

    @mcp.tool()
    def memory_status() -> str:
        """记忆库状态统计"""
        s = stats()
        return (f"chunks: {s['chunks']}, sources: {s['sources']}, "
                f"namespaces: {s['by_namespace']}")

    @mcp.tool()
    def kg_query(entity: str, namespace: str = None, direction: str = "both") -> str:
        """查询知识图谱中实体的关系。entity=实体名, namespace=分区(可选), direction=outgoing/incoming/both"""
        from .kg import kg_query as _kg_query
        result = _kg_query(entity, namespace=namespace, direction=direction)
        if not result["relations"]:
            return f"实体「{entity}」暂无关系"
        lines = [f"{entity} (type={result.get('type')})"]
        for r in result["relations"]:
            arrow = "→" if r["direction"] == "outgoing" else "←"
            other = r["object"] if r["direction"] == "outgoing" else r["object"]
            lines.append(f"  {entity} {arrow} {other} [{r['predicate']}]")
        return "\n".join(lines)

    @mcp.tool()
    def kg_add_fact(subject: str, predicate: str, object: str, namespace: str = "default") -> str:
        """手动写入知识图谱事实（三元组）。subject=主体, predicate=关系, object=客体"""
        from .kg import kg_add
        r = kg_add(subject, predicate, object, namespace=namespace)
        return f"KG: {r.get('status')} — {subject} {predicate} {object}"

    @mcp.tool()
    def kg_invalidate(subject: str, predicate: str, object: str = None,
                      namespace: str = "default") -> str:
        """显式将 KG 事实标记为过时（时间线修正）。
        subject=主体, predicate=关系, object=客体(可选，不传则失效该主体+关系的全部有效事实)。
        事实变化协议：先失效旧事实，再 kg_add 新事实"""
        from .kg import kg_invalidate as _kg_invalidate
        r = _kg_invalidate(subject, predicate, object=object or None, namespace=namespace)
        if r.get("status") == "invalidated":
            return f"✅ 已失效 {r['count']} 条: {subject} {predicate} {object or '*'}"
        return f"ℹ️ 无可失效事实 (noop): {subject} {predicate} {object or '*'}"

    @mcp.tool()
    def kg_stats(namespace: str = None) -> str:
        """KG 统计：实体数 / 三元组数 / 过时事实数 / predicate 分布。namespace=分区(可选)"""
        from .kg import kg_stats as _kg_stats
        s = _kg_stats(namespace=namespace)
        lines = [f"KG 统计{'(分区: ' + namespace + ')' if namespace else ''}:",
                 f"- 实体: {s['entities']}", f"- 三元组: {s['triples']}",
                 f"- 过时事实: {s['expired']}"]
        if s["predicates"]:
            lines.append("- predicate 分布: " + ", ".join(
                f"{p['predicate']}×{p['count']}" for p in s["predicates"][:8]))
        return "\n".join(lines)

    @mcp.tool()
    def kg_timeline(entity: str, namespace: str = None) -> str:
        """实体时间线：按生效时间排序的全部事实（含已过时）。entity=实体名, namespace=分区(可选)"""
        from .kg import kg_timeline as _kg_timeline
        rows = _kg_timeline(entity, namespace=namespace)
        if not rows:
            return f"实体「{entity}」暂无时间线记录"
        lines = [f"实体「{entity}」时间线 ({len(rows)} 条):"]
        for r in rows:
            mark = " " if r["valid_to"] is None else f" → 过时({r['valid_to']})"
            lines.append(f"- [{r['valid_from']}] {r['subject']} {r['predicate']} {r['object']}{mark}")
        return "\n".join(lines)

    @mcp.tool()
    def diary_write(agent_name: str, entry: str, topic: str = "general",
                    namespace: str = "default") -> str:
        """写入一条 agent 个人日记（agent 的观察/想法/工作记录，按日期累积）。
        agent_name=代理名(自动小写归一化), entry=日记内容, topic=主题(默认general), namespace=分区"""
        import time as _t
        conn = _conn()
        agent = agent_name.strip().lower()
        if not agent or not entry or not entry.strip():
            conn.close()
            return "❌ agent_name 和 entry 必填"
        import datetime as _dt
        today = _dt.date.today().isoformat()
        try:
            conn.execute(
                "INSERT INTO diary(namespace, agent_name, topic, content, date, created_at) "
                "VALUES(?,?,?,?,?,?)",
                (namespace, agent, topic.strip() or "general", entry.strip(), today, _t.time()))
            conn.commit()
        finally:
            conn.close()
        return f"✅ 日记已写入: {agent}/{topic} @ {today}"

    @mcp.tool()
    def diary_read(agent_name: str = None, namespace: str = None, limit: int = 20,
                   since: str = None) -> str:
        """读取 agent 日记。agent_name=代理名(可选,不传则全部), namespace=分区(可选),
        limit=返回条数, since=起始日期 YYYY-MM-DD(可选)"""
        conn = _conn()
        try:
            sql = "SELECT agent_name, topic, content, date FROM diary"
            conds, params = [], []
            if agent_name:
                conds.append("agent_name=?")
                params.append(agent_name.strip().lower())
            if namespace:
                conds.append("namespace=?")
                params.append(namespace)
            if since:
                conds.append("date>=?")
                params.append(since)
            if conds:
                sql += " WHERE " + " AND ".join(conds)
            sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
            params.append(int(limit))
            rows = conn.execute(sql, params).fetchall()
            if not rows:
                return f"暂无日记记录"
            lines = []
            for r in rows:
                lines.append(f"[{r['date']}] ({r['agent_name']}/{r['topic']}) {r['content']}")
            return "\n".join(lines)
        finally:
            conn.close()

    @mcp.tool()
    def wake_up(namespace: str = None, max_tokens: int = 900) -> str:
        """生成唤醒上下文（L0 身份 + L1 精华，600-900 tokens，95% 上下文留给对话）。
        namespace=分区(可选,默认全库), max_tokens=输出上限"""
        from .wakeup import wake_up as _wake
        r = _wake(namespace=namespace or None, max_tokens=max_tokens)
        return r["text"] + f"\n\n--- {r['tokens']} tokens / {max_tokens} 上限 ---"

    return mcp


def main(provider):
    db.init_db()
    mcp = build_mcp_server(provider)
    # 审计🟡修复（2026-08-11）：stdio 协议通道必须干净，启动横幅走 stderr
    print("🚀 memory-server MCP: 等待 Agent 连接 (stdio)", file=sys.stderr)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main(None)
