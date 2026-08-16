#!/usr/bin/env python3
"""
memory-server 对话接入器（F1，完整版 2026-08-09）
====================================================
对标旧系统 save_dialogue.py v8：自动发现宿主 agent session 文件 → 增量读取 →
内容净化（英文独白/过程叙述/零信息句过滤）→ 提纯后落原始层 → 幂等摄入。

解决完整版定义第 1/7/8 条：
  - 自动存储完整有效对话内容（净化 + 原文保留）
  - 任意 agent 零配置接入（自动发现 session 目录）
  - **所有 agent 共用一个浅层记忆（namespace="dialogue"）**——对话原文统一存放，
    不再每个 agent 一个记忆分区；来源 agent 记在 speaker 字段（可追溯）

配置（环境变量）：
  MEMORY_AGENT_INGEST=1         # 开启对话接入（service 中作为调度任务）
  MEMORY_AGENTS_DIR=<path>      # agent sessions 根目录（默认 ~/.openclaw/agents）
状态：~/.memory-server/agent_ingest_state.json（每个 session 文件的读取位置）
"""
import os
import re
import sys
import json
import glob
import time
import hashlib
import logging
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory_server import db  # noqa: E402

log = logging.getLogger("memory-server.agent_ingest")

STATE_DIR = os.path.join(os.path.expanduser("~"), ".memory-server")
STATE_FILE = os.environ.get("MEMORY_AGENT_STATE",
                            os.path.join(STATE_DIR, "agent_ingest_state.json"))
DEFAULT_AGENTS_DIR = os.path.expanduser("~/.openclaw/agents")
UTC8 = timezone(timedelta(hours=8))
MAX_TEXT = 20000
WINDOW_H = int(os.environ.get("MEMORY_INGEST_WINDOW_H", "24"))  # 只处理 24h 内消息（首跑窗口）

# ===== 净化规则（从 save_dialogue.py v8 / refine_dialogue.py 搬入，能力等价） =====
REMOVE_PATTERNS = [
    # 2026-08-11 R3：检索噪声过滤（工具调用/命令输出/JSON 碎片，旧版无此规则）
    r'\[TOOL_CALL[:：].*',
    r'\[TOOL_RESULT[:：].*',
    r'\[tool_call[:：].*',
    r'\[tool_result[:：].*',
    r'^```(?:json|bash|sh|python|sql)?\s*$',
    r'^\{".*":.*\}$',  # JSON 配置块
    r'^\[.*relevant_ids.*\]$',
    r'^drawer_\w+\s*\|',  # 表格残骸
    r'^\$\s*[a-zA-Z_/].*',  # shell 命令行
    r'^\d+\s+[a-zA-Z/_.-]+\s+\d+[KMG]?\s+\d+',  # ps/ls 输出残片
    r'让我.*(测试|查[一看了]|试[一试]|检查|确认|验证|尝试|点击|打开|启动|刷新|截图|分析)',
    r'继续.*(第|页|收集|查看|测试|翻|下一页)',
    r'正在.*(浏览|扫描|读取|分析|索引|检查|测试|查询|搜索|等待)',
    r'试试.*(用|看|点击|导航|搜索|这个方法)',
    r'试一下|看一看|查一下|做一下|跑一下',
    r'先.*(试|查|看|检查|验证|确认|做)',
    r'已经.*(好了|完成|做完|查到|找到|搞定)',
    r'命令.*(还在|执行中|运行中)',
    r'数据在积累|进程已|再等等|等一下|稍等|这个过程可能需要',
    r'^好的[！。!]?$|^收到[！。!]?$|^明白了[！。!]?$|^可以了[！。!]?$',
    r'^完成[了]?[！。!]?$|^已写入[！。!]?$|^已添加[！。!]?$',
]
KEEP_PATTERNS = [
    r'✅|❌|⚠️|🔴|🟡|🟢',
    r'结论|决定|方案|规则|修正|纠正|教训|注意|建议|总结|报告|问题',
    r'发现[了：:]|找到[了：:]|不(对|行|能|可以|应该)|应该|需要|必须|因为|所以',
    r'分析|对比|差异|区别|建议方案',
    r'^\|.*\|.*\|',
    r'^##',
    r'^```',
]
ENGLISH_MONOLOGUE_PREFIXES = [
    'Let me', 'Good,', 'Good.', 'Good ', 'Now I', 'I need to', 'First,',
    'The user is', 'OK,', 'Alright,', 'Great!', 'Perfect!',
    "Let's", 'I should', 'I can', 'I will', 'Maybe I', 'I think',
    'But wait', 'Actually,', 'Hmm,', 'Wait,', 'Oh,',
    'The error', 'This is', 'Here is', 'That is',
]


def is_english_monologue(text):
    text = text.strip()
    if not text:
        return False
    if re.search(r'[\u4e00-\u9fff]', text):
        return False
    if text.startswith('[Inter-session message]'):
        return True
    for p in ENGLISH_MONOLOGUE_PREFIXES:
        if text.startswith(p):
            return True
    return False


def should_keep(text):
    if is_english_monologue(text):
        return False
    if len(text) > 100 or text.startswith("##") or text.startswith("```"):
        return True
    for p in KEEP_PATTERNS:
        if re.search(p, text):
            return True
    return False


def should_remove(text):
    if is_english_monologue(text):
        return True
    for p in REMOVE_PATTERNS:
        if re.search(p, text):
            return True
    return False


def purify_assistant_text(text):
    """提纯助手回复：去英文独白和过程叙述（能力等价 save_dialogue v8）"""
    if not text:
        return None
    cleaned = re.sub(r'^[A-Za-z][A-Za-z\s,\.!?;:\'\"\(\)-]+(?=[\u4e00-\u9fff])', '', text.strip())
    if cleaned.strip():
        text = cleaned
    if should_remove(text) and not should_keep(text):
        return None
    return text


def extract_user_text(content):
    if isinstance(content, list):
        text = "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    elif isinstance(content, str):
        text = content
    else:
        return None
    text = text.strip()
    if not text:
        return None
    if text.startswith("System") or "[cron:" in text:
        return None
    m = re.search(r'\[(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) [^\]]+\]\n*(.+?)$', text, re.DOTALL)
    if m:
        result = m.group(1).strip()
        if result and len(result) > 5:
            return result
    lines = text.split("\n")
    clean = [l.strip() for l in lines
             if l.strip() and not (l.startswith('{') and l.endswith('}'))
             and not l.startswith('```')]
    if clean:
        result = "\n".join(clean)
        if not any(x in result for x in ['"label":', '"id":', '"schema":', '"channel":', '"provider"']):
            return result
    return text[:2000]


def extract_assistant_text(content):
    if isinstance(content, list):
        parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
        return "\n".join(parts).strip() if parts else None
    if isinstance(content, str):
        return content.strip() or None
    return None


def is_recent(ts_str, window_h=None):
    """是否在窗口期内（默认 24h；首跑全量可用大窗口）"""
    window_h = window_h or WINDOW_H
    try:
        dt = datetime.fromisoformat(ts_str.rstrip("Z")).replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() < window_h * 3600
    except Exception:
        return True


# ===== 状态管理（增量位置追踪） =====

def _load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ===== 发现与摄入 =====

def discover_session_files(agents_dir=None):
    """发现所有 agent 的 session 文件: [(path, agent_id)]"""
    agents_dir = agents_dir or os.environ.get("MEMORY_AGENTS_DIR", DEFAULT_AGENTS_DIR)
    if not os.path.isdir(agents_dir):
        return []
    files = []
    for agent_dir in sorted(glob.glob(os.path.join(agents_dir, "*"))):
        if not os.path.isdir(agent_dir):
            continue
        agent_id = os.path.basename(agent_dir)
        sessions = os.path.join(agent_dir, "sessions")
        if os.path.isdir(sessions):
            for f in sorted(glob.glob(os.path.join(sessions, "*.jsonl"))):
                if f.endswith(".trajectory.jsonl"):
                    continue  # 跳过轨迹文件（含过程噪声）
                files.append((f, agent_id))
    return files


def ingest_agent_sessions(provider, agents_dir=None, window_h=None, dry_run=False,
                          namespace="dialogue"):
    """增量读取 → 净化 → 提纯落原始层 → 幂等摄入。

    所有 agent 的对话写入同一个浅层 namespace（默认 "dialogue" 共享层），
    来源 agent 记录在 JSONL speaker 字段。返回统计。
    """
    files = discover_session_files(agents_dir)
    if not files:
        return {"agents": 0, "messages": 0, "purged": 0, "saved": 0, "skipped": 0}

    state = _load_state()
    stats = {"agents": len(set(a for _, a in files)), "messages": 0,
             "purged": 0, "saved": 0, "skipped": 0}
    # 审计🔵修复（2026-08-11 复审）：原创建了从未使用的 conn，移除
    for filepath, agent_id in files:
        fname = os.path.basename(filepath)
        state_key = f"{agent_id}:{fname}"
        last_pos = state.get(state_key, 0)
        cur = os.path.getsize(filepath)
        if cur <= last_pos:
            continue

        with open(filepath, encoding="utf-8", errors="ignore") as f:
            if last_pos > 0:
                f.seek(last_pos)
            raw = f.read()

        entries = []  # 提纯后的 (timestamp_iso, role, speaker, text)
        purged = 0
        for line in raw.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if entry.get("type") != "message":
                continue
            ts = entry.get("timestamp", "")
            if not is_recent(ts, window_h):
                continue
            msg = entry.get("message", {})
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role not in ("user", "assistant"):
                continue

            if role == "user":
                text = extract_user_text(content)
                if not text:
                    continue
                if text.startswith("[Inter-session message]") or \
                        "This content was routed by OpenClaw" in text:
                    purged += 1
                    continue
                purified = text
            else:
                text = extract_assistant_text(content)
                if not text or text.strip() == "NO_REPLY":
                    continue
                purified = purify_assistant_text(text)
                if not purified:
                    purged += 1
                    continue

            if len(purified) > MAX_TEXT:
                purified = purified[:MAX_TEXT] + "\n...[截断]"
            entries.append((ts, role, agent_id, purified))

        if entries:
            stats["messages"] += len(entries)
            stats["purged"] += purged
            # 按日期分组写原始层 JSONL（提纯后，L0 原文层）
            by_day = {}
            for ts, role, _, text in entries:
                day = _day_of(ts)
                by_day.setdefault(day, []).append(
                    {"role": role, "speaker": agent_id, "content": text,
                     "timestamp": _iso(ts), "session": fname.replace(".jsonl", "")})
            for day, msgs in by_day.items():
                raw_path = _write_raw(namespace, day, msgs)
                if dry_run:
                    stats["saved"] += len(msgs)
                    continue
                from memory_server.ingest import ingest_jsonl
                r = ingest_jsonl(raw_path, namespace, provider)
                if r["status"] == "ok":
                    stats["saved"] += len(msgs)
                else:
                    stats["skipped"] += len(msgs)

        state[state_key] = cur
    _save_state(state)
    return stats


def _day_of(ts_str):
    try:
        dt = datetime.fromisoformat(ts_str.rstrip("Z")).replace(tzinfo=timezone.utc)
        return dt.astimezone(UTC8).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now(UTC8).strftime("%Y-%m-%d")


def _iso(ts_str):
    try:
        dt = datetime.fromisoformat(ts_str.rstrip("Z")).replace(tzinfo=timezone.utc)
        return dt.astimezone(UTC8).isoformat()
    except Exception:
        return ts_str


def _write_raw(ns, day, msgs):
    """提纯后的消息写入原始层 raw/<ns>/<day>.jsonl（L0 原文层，一天一个文件）

    2026-08-10 修复：原实现按内容 md5 命名 → 每次执行生成新文件（一天 N 个），
    导致 Active Memory 索引风暴（extraPaths 指向 raw/ 时每 5 分钟触发重建）。
    改为按天固定文件名 + 行级去重合并，与旧系统 save_dialogue「一天一文件」一致。
    """
    from memory_server.ingest import RAW_DIR
    ns_dir = os.path.join(RAW_DIR, ns)
    os.makedirs(ns_dir, exist_ok=True)
    path = os.path.join(ns_dir, f"{day}.jsonl")
    # 读已有行（去重基准）
    existing = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    existing.add(line)
    # 合并新消息（按行内容去重）
    new_lines = []
    for m in msgs:
        line = json.dumps(m, ensure_ascii=False)
        if line not in existing:
            new_lines.append(line)
            existing.add(line)
    if new_lines:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(new_lines) + "\n")
    return path


def main():
    import argparse
    parser = argparse.ArgumentParser(prog="eidetic agent-ingest",
                                     description="对话接入器：监听 agent session → 净化 → 摄入")
    parser.add_argument("--agents-dir", default=None, help="agent sessions 根目录（默认 ~/.openclaw/agents）")
    parser.add_argument("--window-h", type=int, default=None, help="时间窗口（小时，默认 24）")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写入")
    parser.add_argument("--fts-only", action="store_true", help="仅关键词模式")
    parser.add_argument("--ns", default="dialogue", help="浅层 namespace（默认 dialogue 共享层）")
    args = parser.parse_args()

    db.init_db()
    if args.dry_run:
        provider = None
    else:
        from memory_server.embed import detect_provider
        provider = detect_provider(prefer="fts-only" if args.fts_only else None)
    stats = ingest_agent_sessions(provider, agents_dir=args.agents_dir,
                                  window_h=args.window_h, dry_run=args.dry_run,
                                  namespace=args.ns)
    print(f"📥 对话接入: {stats['agents']} agents / 消息 {stats['messages']} "
          f"/ 净化过滤 {stats['purged']} / 已存 {stats['saved']} / 跳过 {stats['skipped']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
