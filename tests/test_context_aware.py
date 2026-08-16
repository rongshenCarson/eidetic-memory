"""场景感知自动化测试（2026-08-11，混合型 ④：③显式 > ②会话记忆 > ①KG兜底）"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import memory_server.mcp_api as mcp_api
from memory_server import db
from memory_server.mcp_api import _resolve_task_context


def _reset():
    mcp_api._session_task_context = None
    mcp_api._session_ctx_ts = 0.0


def test_kg_fallback():
    """① KG 兜底：查询命中实体自动提取，未命中返回空"""
    _reset()
    conn = db.get_conn()
    conn.execute("INSERT INTO entities(name, aliases, canonical) VALUES(?,?,?)", ("acme", "[]", "acme"))
    conn.commit()
    # 命中：acme是明确实体
    assert _resolve_task_context(None, "acme 产地", conn) == "acme"
    # 未命中：泛词不兜底
    assert _resolve_task_context(None, "完全不存在的内容xyz", conn) == ""


def test_explicit_overrides():
    """③ 显式传优先，并写入会话记忆"""
    _reset()
    conn = db.get_conn()
    assert _resolve_task_context("geo-card", "筛选条件", conn) == "geo-card"
    # 会话记忆已被显式值覆盖
    assert mcp_api._session_task_context == "geo-card"


def test_session_memory_reuse():
    """② 会话记忆：未显式传时复用上次上下文"""
    _reset()
    conn = db.get_conn()
    _resolve_task_context("tomango", "五层架构", conn)
    assert _resolve_task_context(None, "芒格", conn) == "tomango"


def test_clear_context():
    """空串清除会话记忆，回落到 KG 兜底"""
    _reset()
    conn = db.get_conn()
    conn.execute("INSERT INTO entities(name, aliases, canonical) VALUES(?,?,?)", ("acme", "[]", "acme"))
    conn.commit()
    _resolve_task_context("tomango", "五层架构", conn)
    assert _resolve_task_context("", "五层架构", conn) == ""
    # 清除后 KG 兜底生效
    assert _resolve_task_context(None, "acme 品牌", conn) == "acme"
