"""对话接入器测试（F1）：净化/幂等/增量/位置追踪"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone

import pytest

from memory_server import db
from memory_server.embed import FtsOnlyProvider


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    """隔离状态文件 + 隔离 agents 目录"""
    from memory_server import agent_ingest as ai
    monkeypatch.setattr(ai, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(ai, "DEFAULT_AGENTS_DIR", str(tmp_path / "agents"))
    db.init_db()


def _write_session(agents_dir, agent_id, lines, fname="s1.jsonl"):
    d = os.path.join(agents_dir, agent_id, "sessions")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, fname)
    with open(p, "w", encoding="utf-8") as f:
        for l in lines:
            f.write(json.dumps(l, ensure_ascii=False) + "\n")
    return p


def _msg(role, content, ts=None):
    return {"type": "message", "timestamp": ts or datetime.now(timezone.utc).isoformat(),
            "message": {"role": role, "content": content}}


def test_purify_and_ingest(tmp_path):
    from memory_server import agent_ingest as ai
    now = datetime.now(timezone.utc).isoformat()
    _write_session(tmp_path, "main", [
        _msg("user", "今天讨论了完整版方案", now),
        _msg("assistant", "Let me check first.\n好的，F1是对话接入器。", now),
        _msg("assistant", "让我试一下看看这个命令能不能跑", now),  # 「不+能」KEEP → 保留（与旧系统一致）
        _msg("assistant", "NO_REPLY", now),                        # 过滤
        _msg("user", "[Inter-session message] 元数据", now),      # 过滤
    ])
    stats = ai.ingest_agent_sessions(FtsOnlyProvider(), agents_dir=tmp_path)
    assert stats["messages"] == 3 and stats["purged"] == 1
    conn = db.get_conn()
    rows = conn.execute("SELECT text FROM chunks").fetchall()
    conn.close()
    assert len(rows) == 3
    texts = [r[0] for r in rows]
    assert any("F1是对话接入器" in t for t in texts)  # 英文独白前缀被剥离，中文保留
    assert any("完整版方案" in t for t in texts)


def test_idempotent_and_incremental(tmp_path):
    from memory_server import agent_ingest as ai
    p = _write_session(tmp_path, "main", [_msg("user", "第一条")])
    ai.ingest_agent_sessions(FtsOnlyProvider(), agents_dir=tmp_path)
    # 重跑：位置未变 → 0 新增
    s2 = ai.ingest_agent_sessions(FtsOnlyProvider(), agents_dir=tmp_path)
    assert s2["messages"] == 0
    # 追加一条 → 只处理新增
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(_msg("user", "第二条"), ensure_ascii=False) + "\n")
    s3 = ai.ingest_agent_sessions(FtsOnlyProvider(), agents_dir=tmp_path)
    assert s3["messages"] == 1
    conn = db.get_conn()
    n = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    conn.close()
    assert n == 2


def test_trajectory_skipped(tmp_path):
    from memory_server import agent_ingest as ai
    _write_session(tmp_path, "main", [_msg("user", "正常对话")], fname="a.jsonl")
    _write_session(tmp_path, "main", [_msg("user", "轨迹噪声")], fname="a.trajectory.jsonl")
    stats = ai.ingest_agent_sessions(FtsOnlyProvider(), agents_dir=tmp_path)
    assert stats["messages"] == 1  # trajectory 被跳过


def test_multiple_agents_share_shallow_layer(tmp_path):
    """所有 agent 共用一个浅层记忆（namespace=dialogue），来源记在 speaker 字段"""
    from memory_server import agent_ingest as ai
    _write_session(tmp_path, "main", [_msg("user", "主人格对话")])
    _write_session(tmp_path, "1", [_msg("user", "技术agent对话")])
    _write_session(tmp_path, "3", [_msg("user", "创意agent对话")])
    ai.ingest_agent_sessions(FtsOnlyProvider(), agents_dir=tmp_path)
    conn = db.get_conn()
    nss = {r[0] for r in conn.execute("SELECT DISTINCT namespace FROM chunks").fetchall()}
    n = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    conn.close()
    # 关键：三个 agent 的对话都在同一个浅层，不分区
    assert nss == {"dialogue"}, f"应共用一个浅层，实际: {nss}"
    assert n == 3
    # 来源可追溯：raw JSONL 保留 speaker 字段（读 RAW_DIR，跟随 MEMORY_SERVER_RAW_DIR 隔离）
    from memory_server.ingest import RAW_DIR
    raw_dir = os.path.join(RAW_DIR, "dialogue")
    raw_files = os.listdir(raw_dir)
    assert raw_files, "raw 浅层应有 JSONL 文件"
    with open(os.path.join(raw_dir, raw_files[0])) as f:
        content = f.read()
        assert "speaker" in content, "speaker 字段应保留（来源可追溯）"
