#!/usr/bin/env python3
"""
memory-server 摄取管道（P1a）
==============================
职责：对话/文件导入 → 原始层落盘 → 分块 → 嵌入 → 入库（幂等）
"""
import os
import re
import time
import json
import hashlib

from . import db
from .embed import EmbeddingProvider

RAW_DIR = os.environ.get("MEMORY_SERVER_RAW_DIR",
                  os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "raw"))

CHUNK_TOKENS = 400
CHUNK_OVERLAP = 80
MIN_CHUNK_LEN = 50  # 2026-08-11 R3：最小语义长度（低于此的碎片不入库，避免检索噪声）


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def chunk_text(text, tokens=CHUNK_TOKENS, overlap=CHUNK_OVERLAP, min_len=MIN_CHUNK_LEN):
    """按字符近似分块（中文场景：~1.5 字符/token，按 2 字符保守切）

    min_len: 最小语义长度（2026-08-11 R3：低于阈值的碎片不入库，避免检索噪声）
    短文本（< step）整体保留——完整语义句不因短而被滤（碎片是长文本切出的残块）
    """
    step = tokens * 2
    ov = overlap * 2
    if len(text.strip()) < step:
        return [text] if len(text.strip()) >= 10 else []
    chunks = []
    i = 0
    while i < len(text):
        piece = text[i:i + step]
        if len(piece.strip()) >= min_len:
            chunks.append(piece)
        if i + step >= len(text):
            break
        i += step - ov
    return chunks or []


# 审计四审安全🟡（2026-08-11）：敏感密钥模式——摄入时打码，防止明文密钥入库并随备份扩散
SECRET_PATTERNS = [
    (r'sk-[A-Za-z0-9]{16,}', "sk-API密钥"),
    (r'Bearer\s+[A-Za-z0-9._-]{16,}', "Bearer令牌"),
    (r'AKIA[0-9A-Z]{16}', "AWS密钥"),
    # 审计八审🔵（2026-08-12）：保留键名只码值（原整体替换丢字段名上下文）
    (r'((?:api[_-]?key|token|secret|password|passwd)\s*[=:：]\s*)[A-Za-z0-9._-]{8,}', "凭证字段"),
]


def _redact_secrets(text):
    """密钥打码：sk-xxx → sk-***（保留前缀可识别，抹掉明文）
    审计八审🔵：凭证字段模式保留键名（api_key=xxx → api_key=***）"""
    for pattern, label in SECRET_PATTERNS:
        def _mask(m):
            if m.groups() and m.group(1) is not None:
                return m.group(1) + "***"  # 保留键名，只码值
            return m.group(0)[:3] + "***"
        text = re.sub(pattern, _mask, text)
    return text


def ingest_file(path, namespace="default", provider=None, embed=True, skip_copy=False):
    """摄入单个文件（幂等：内容 hash 去重）

    skip_copy=True: 文件已在 raw/ 目录（由 importer 复制），跳过二次落盘
    """
    conn = db.get_conn()
    try:
        h = file_hash(path)
        # 幂等：同 hash 已摄入则跳过
        dup = conn.execute("SELECT 1 FROM ingested_hashes WHERE hash=?", (h,)).fetchone()
        if dup:
            return {"status": "skip", "path": path, "reason": "duplicate"}

        rel = os.path.basename(path)
        if skip_copy:
            raw_path = path
        else:
            # 原始层落盘（source of truth）
            os.makedirs(os.path.join(RAW_DIR, namespace), exist_ok=True)
            raw_path = os.path.join(RAW_DIR, namespace, f"{int(time.time())}-{rel}")
            with open(path, "rb") as src, open(raw_path, "wb") as dst:
                dst.write(src.read())

        # 分块 + 嵌入（审计七审🟡：ingest_file 路径也做密钥打码——原只 jsonl 覆盖）
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        text = _redact_secrets(text)
        pieces = chunk_text(text)
        vecs = provider.embed(pieces) if (embed and provider and provider.dim) else [[] for _ in pieces]

        cur = conn.execute(
            "INSERT INTO sources(namespace, path, hash, mtime, size) VALUES(?,?,?,?,?)",
            (namespace, raw_path, h, time.time(), os.path.getsize(path)),
        )
        sid = cur.lastrowid
        t0 = time.time()
        for i, (piece, vec) in enumerate(zip(pieces, vecs)):
            conn.execute(
                "INSERT INTO chunks(source_id, namespace, path, start_line, end_line, text, embedding, updated_at, metadata) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (sid, namespace, raw_path, i, i, piece,
                 db.blob_encode(vec) if vec else None, t0,
                 json.dumps({"seq": i})),
            )
        conn.execute("INSERT INTO ingested_hashes(hash, path, ingested_at) VALUES(?,?,?)", (h, path, time.time()))
        conn.commit()
        return {"status": "ok", "path": path, "chunks": len(pieces), "namespace": namespace}
    finally:
        conn.close()


def ingest_dir(dir_path, namespace="default", provider=None, pattern=(".md", ".jsonl")):
    """摄入目录下所有匹配文件"""
    results = []
    for root, _, files in os.walk(dir_path):
        for fn in sorted(files):
            if fn.endswith(pattern):
                results.append(ingest_file(os.path.join(root, fn), namespace, provider))
    return results


def ingest_jsonl(path, namespace="default", provider=None):
    """摄入对话 JSONL（每行一条消息）——行级幂等

    2026-08-10 改造（对齐 save_dialogue 一天一文件）：
    - 一天一文件（追加式）→ 文件 hash 会变，改为行级 hash 幂等，只摄入新行
    - path 已在 raw/ 下时不再二次复制（消除 raw 文件爆炸：原实现每摄入一次
      复制一个带时间戳副本）
    """
    conn = db.get_conn()
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = [l.strip() for l in f if l.strip()]
        if not lines:
            return {"status": "skip", "reason": "empty"}

        # 行级幂等：只摄入新行（审计六审 P1：改批量 IN 点查——原全量 SELECT 装载
        # line-hash 集合，100 万行 ≈ 760MB 内存 + 每 5min 重建；现只查本轮新行）
        line_hashes = [hashlib.sha256(l.encode()).hexdigest() for l in lines]
        # 分批点查（每批 500），只查本轮行是否已存在
        ingested = set()
        B = 500
        for i in range(0, len(line_hashes), B):
            chunk_h = line_hashes[i:i + B]
            ph = ",".join("?" * len(chunk_h))
            rows = conn.execute(
                f"SELECT hash FROM ingested_hashes WHERE hash IN ({ph})", chunk_h).fetchall()
            ingested |= {r[0] for r in rows}
        new_idx = [i for i, h in enumerate(line_hashes) if h not in ingested]
        if not new_idx:
            return {"status": "skip", "reason": "all lines ingested"}

        # raw_path：已在 raw/ 下直接复用，否则落盘
        raw_root = os.path.abspath(RAW_DIR)
        if os.path.abspath(os.path.dirname(path)).startswith(raw_root):
            raw_path = path
        else:
            os.makedirs(os.path.join(RAW_DIR, namespace), exist_ok=True)
            raw_path = os.path.join(RAW_DIR, namespace, f"{int(time.time())}-{os.path.basename(path)}")
            with open(path, "rb") as src, open(raw_path, "wb") as dst:
                dst.write(src.read())

        # source：先查后插（同文件追加时复用，避免 UNIQUE 冲突）
        src = conn.execute("SELECT id FROM sources WHERE namespace=? AND path=?",
                           (namespace, raw_path)).fetchone()
        if src:
            sid = src["id"]
        else:
            h = file_hash(raw_path)
            cur = conn.execute(
                "INSERT INTO sources(namespace, path, hash, mtime, size) VALUES(?,?,?,?,?)",
                (namespace, raw_path, h, time.time(), os.path.getsize(path)))
            sid = cur.lastrowid

        # 只嵌入新行（2026-08-11 R3：入库前过滤检索噪声——工具调用/命令/JSON碎片/过短消息）
        kept = []  # [(原始行索引, 文本)]，过滤后与 new_idx 解耦
        _NOISE_RE = re.compile(
            r'\[TOOL_CALL[:：]|\[TOOL_RESULT[:：]|\[tool_call[:：]|\[tool_result[:：]|'
            r'relevant_ids|drawer_\w+\s*\||^```|^\{".*":.*\}$|^\$\s*[a-zA-Z_/]|'
            r'^\d+-\d+-\d+T\d+:\d+.*\[(INFO|WARN|ERROR|DEBUG)\]|'
            r'^\[?(INFO|WARN|ERROR|DEBUG)\]')
        for i in new_idx:
            line = lines[i]
            try:
                obj = json.loads(line)
                content = f"{obj.get('role','')}: {obj.get('content','')}"
            except Exception:
                content = line
            c = content.strip()
            # 过滤：噪声模式（工具调用/命令/JSON 碎片/日志；短消息不过滤——
            # 语义价值由上游净化层判断，切分碎片由 chunk_text MIN_CHUNK_LEN 拦截）
            if not c or _NOISE_RE.search(c[:300]):
                continue
            # 审计四审安全🟡：密钥打码后入库（防明文密钥随备份扩散）
            c = _redact_secrets(c)
            kept.append((i, c))
        texts = [t for _, t in kept]
        vecs = provider.embed(texts) if (provider and provider.dim) else [[] for _ in texts]

        t0 = time.time()
        for k, (i, _) in enumerate(kept):
            conn.execute(
                "INSERT INTO chunks(source_id, namespace, path, start_line, end_line, text, embedding, updated_at, metadata) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (sid, namespace, raw_path, i, i, texts[k],
                 db.blob_encode(vecs[k]) if vecs[k] else None, t0,
                 json.dumps({"type": "message"})))
        for i, _ in kept:
            conn.execute("INSERT INTO ingested_hashes(hash, path, ingested_at) VALUES(?,?,?)",
                         (line_hashes[i], f"line:{namespace}", time.time()))
        # 审计🔵修复（2026-08-11 复审）：被噪声过滤的行也必须标记 ingested——
        # 否则这些行永远被视为"新行"，每 5 分钟重扫同一批噪声（纯性能浪费）。
        # 行内容不变则 hash 不变，标记后不会误跳过未来真正的新内容。
        for i in new_idx:
            conn.execute("INSERT OR IGNORE INTO ingested_hashes(hash, path, ingested_at) VALUES(?,?,?)",
                         (line_hashes[i], f"line:{namespace}", time.time()))
        conn.commit()
        return {"status": "ok", "path": path, "messages": len(texts)}
    finally:
        conn.close()
