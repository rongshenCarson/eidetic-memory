#!/usr/bin/env python3
"""
memory-server 存储层 — 单库 SQLite（P1a）
==========================================
schema: sources / chunks / chunks_fts / meta / ingested_hashes / watermarks
"""
import os
import sqlite3
import time
import struct
import json

DB_DIR = os.environ.get("MEMORY_SERVER_DB_DIR",
                      os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
DB_PATH = os.path.join(DB_DIR, "memory.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace TEXT NOT NULL DEFAULT 'default',
    path TEXT NOT NULL,
    hash TEXT,
    mtime REAL,
    size INTEGER,
    UNIQUE(namespace, path)
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER REFERENCES sources(id),
    namespace TEXT NOT NULL DEFAULT 'default',
    path TEXT NOT NULL,
    start_line INTEGER,
    end_line INTEGER,
    text TEXT NOT NULL,
    embedding BLOB,
    updated_at REAL NOT NULL,
    metadata TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_chunks_namespace ON chunks(namespace);
CREATE INDEX IF NOT EXISTS idx_chunks_updated ON chunks(updated_at);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, content='chunks', content_rowid='id', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE OF text ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES('delete', old.id, old.text);
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES('delete', old.id, old.text);
END;
CREATE TABLE IF NOT EXISTS meta (
    namespace TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    dim INTEGER NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ingested_hashes (
    hash TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    ingested_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS watermarks (
    task TEXT PRIMARY KEY,
    last_successful_run REAL NOT NULL
);

-- ============ KG 插件（P1 收尾 2026-08-09） ============
CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace TEXT NOT NULL DEFAULT 'default',
    name TEXT NOT NULL,              -- 实际出现的名字（可含别名）
    canonical TEXT NOT NULL,         -- 规范名（归一化后，别名归入同一规范名）
    aliases TEXT DEFAULT '[]',       -- JSON 别名列表
    type TEXT DEFAULT 'entity',
    summary TEXT,                    -- 实体摘要（③b Graphiti 借鉴）
    created_at REAL,
    UNIQUE(namespace, name)
);
CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);

CREATE TABLE IF NOT EXISTS triples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace TEXT NOT NULL DEFAULT 'default',
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    source_id INTEGER,
    created_at REAL,
    UNIQUE(namespace, subject, predicate, object)
);
CREATE INDEX IF NOT EXISTS idx_triples_subject ON triples(subject);
CREATE INDEX IF NOT EXISTS idx_triples_object ON triples(object);
CREATE INDEX IF NOT EXISTS idx_triples_predicate ON triples(predicate);

CREATE TABLE IF NOT EXISTS extracts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace TEXT NOT NULL DEFAULT 'default',
    type TEXT NOT NULL,              -- decision | preference | fact | milestone | episodic | term
    text TEXT NOT NULL,
    short TEXT,                      -- 1 句压缩（召回注入省 token，对标 mem0 风格）
    importance INTEGER DEFAULT 3,    -- 1-5（5=核心决策）
    entities TEXT DEFAULT '[]',      -- JSON 数组（关联实体）
    tags TEXT DEFAULT '[]',          -- JSON 数组
    timestamp TEXT,                  -- YYYY-MM-DD
    source_id INTEGER,
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_extracts_type ON extracts(type);

CREATE TABLE IF NOT EXISTS scenes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace TEXT NOT NULL DEFAULT 'default',
    date TEXT NOT NULL,              -- YYYY-MM-DD
    title TEXT,                      -- 场景名
    content TEXT NOT NULL,           -- 场景归纳全文
    status TEXT DEFAULT 'active',    -- active | done | paused | closed
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_scenes_date ON scenes(date);

CREATE TABLE IF NOT EXISTS persona (
    namespace TEXT PRIMARY KEY,
    content TEXT NOT NULL,           -- 画像全文
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS core_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace TEXT NOT NULL DEFAULT 'default',
    keyword TEXT NOT NULL,           -- 晋升关键词（n-gram）
    freq INTEGER NOT NULL,           -- 出现频次
    text TEXT NOT NULL,              -- 代表记忆文本
    promoted_at REAL,
    UNIQUE(namespace, keyword)       -- 审计🟡修复（2026-08-11）：原无 UNIQUE → INSERT OR IGNORE 失效
);
CREATE INDEX IF NOT EXISTS idx_core_keyword ON core_memories(keyword);

CREATE TABLE IF NOT EXISTS learnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace TEXT NOT NULL DEFAULT 'default',
    code TEXT UNIQUE,                -- LRN-YYYYMMDD-NNN
    title TEXT NOT NULL,
    root_cause TEXT,
    lesson TEXT,
    fix TEXT,
    trigger_when TEXT,
    trigger_do TEXT,
    created_at REAL
);

CREATE TABLE IF NOT EXISTS curated (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace TEXT NOT NULL DEFAULT 'default',
    topic TEXT NOT NULL,             -- 主题（如 brand_core）
    content TEXT NOT NULL,
    updated_at REAL,
    UNIQUE(namespace, topic)
);

CREATE TABLE IF NOT EXISTS reflections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace TEXT NOT NULL DEFAULT 'default',
    date TEXT NOT NULL,              -- YYYY-MM-DD
    content TEXT NOT NULL,
    created_at REAL,
    UNIQUE(namespace, date)
);

CREATE TABLE IF NOT EXISTS diary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace TEXT NOT NULL DEFAULT 'default',
    agent_name TEXT NOT NULL,        -- 小写归一化（对齐旧系统 #1243）
    topic TEXT NOT NULL DEFAULT 'general',
    content TEXT NOT NULL,
    date TEXT NOT NULL,              -- YYYY-MM-DD
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_diary_agent ON diary(namespace, agent_name, created_at);
"""


def get_conn():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    _migrate(conn)
    _set_schema_version(conn)
    conn.commit()
    conn.close()


SCHEMA_VERSION = 2  # 审计四审迁移项（2026-08-11）：schema 版本号，doctor 校验用


def _set_schema_version(conn):
    """写入 schema 版本（审计四审：迁移无版本号 → 失败无感知）"""
    conn.execute(
        "INSERT INTO schema_version(version, updated_at) VALUES(?, ?) ",
        (SCHEMA_VERSION, time.time()))


def get_schema_version():
    """读当前 schema 版本（无记录返回 0 = 旧库）"""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT version FROM schema_version ORDER BY updated_at DESC LIMIT 1").fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0
    finally:
        conn.close()


def _migrate(conn):
    """轻量迁移：已存在的表补新列（2026-08-09 F2 扩展）"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(extracts)").fetchall()}
    additions = {
        "short": "TEXT",
        "importance": "INTEGER DEFAULT 3",
        "entities": "TEXT DEFAULT '[]'",
        "timestamp": "TEXT",
    }
    for col, ddl in additions.items():
        if col not in cols:
            conn.execute(f"ALTER TABLE extracts ADD COLUMN {col} {ddl}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_extracts_importance ON extracts(importance)")
    # F1-F8 后新增列：entities.canonical/aliases/summary（2026-08-10 实体归一化）
    ent_cols = {r[1] for r in conn.execute("PRAGMA table_info(entities)").fetchall()}
    ent_additions = {"canonical": "TEXT", "aliases": "TEXT DEFAULT '[]'", "summary": "TEXT"}
    for col, ddl in ent_additions.items():
        if col not in ent_cols:
            conn.execute(f"ALTER TABLE entities ADD COLUMN {col} {ddl}")
    # 已有数据回填 canonical=name
    conn.execute("UPDATE entities SET canonical=name WHERE canonical IS NULL OR canonical=''")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_entities_canonical ON entities(canonical)")
    # 审计🟡修复（2026-08-11 复审）：core_memories 存量库无 UNIQUE（只进新库 SCHEMA）→
    # INSERT OR IGNORE 对存量用户失效。迁移：去重 → 建唯一索引 → 清停用词垃圾行
    try:
        # 去重：同 (namespace, keyword) 保留最新一条
        conn.execute("""
            DELETE FROM core_memories WHERE id NOT IN (
                SELECT MAX(id) FROM core_memories GROUP BY namespace, keyword)
        """)
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_core_ns_kw "
            "ON core_memories(namespace, keyword)")
        # 清垃圾关键词（与 pipeline.STOPWORDS 对齐）
        from .pipeline import STOPWORDS
        ph = ",".join("?" for _ in STOPWORDS)
        conn.execute(
            f"DELETE FROM core_memories WHERE keyword IN ({ph})", list(STOPWORDS))
    except Exception:
        pass


def blob_encode(vec):
    return struct.pack(f"<{len(vec)}f", *vec)


def blob_decode(blob):
    if not blob:
        return []
    n = len(blob) // 4
    return struct.unpack(f"<{n}f", blob)


def set_watermark(task, ts=None):
    conn = get_conn()
    conn.execute(
        "INSERT INTO watermarks(task, last_successful_run) VALUES(?,?) "
        "ON CONFLICT(task) DO UPDATE SET last_successful_run=excluded.last_successful_run",
        (task, ts or time.time()),
    )
    conn.commit()
    conn.close()


def get_watermark(task):
    conn = get_conn()
    row = conn.execute("SELECT last_successful_run FROM watermarks WHERE task=?", (task,)).fetchone()
    conn.close()
    return row["last_successful_run"] if row else None
