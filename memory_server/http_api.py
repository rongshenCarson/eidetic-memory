#!/usr/bin/env python3
"""memory-server HTTP API（P1a，标准库零依赖，默认 127.0.0.1:8720）"""
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import db
from .search import search, stats

# ---- 场景感知自动化（2026-08-11，混合型 ④）----
# HTTP 无状态，但 serve 是单进程长驻 → 进程级会话记忆对单用户本地场景成立
# ③ 显式传：?task= 参数 | ② 会话记忆：/set-context 设置后复用 | ① KG 实体兜底
_session_ctx = None
_session_ctx_ts = 0.0
_CTX_TTL = 3600


def _resolve_ctx(explicit, query, conn):
    global _session_ctx, _session_ctx_ts
    if explicit is not None:
        if explicit.strip():
            _session_ctx, _session_ctx_ts = explicit.strip(), time.time()
        else:
            _session_ctx = None
        return _session_ctx or ""
    if _session_ctx and (time.time() - _session_ctx_ts) < _CTX_TTL:
        return _session_ctx
    import re
    try:
        for kw in re.split(r"[\s,，。]+|[A-Za-z0-9]+", query):
            kw = kw.strip()
            if len(kw) < 2:
                continue
            row = conn.execute("SELECT name FROM entities WHERE name=? LIMIT 1", (kw,)).fetchone()
            if row and row["name"] not in ("source", "推理"):
                return row["name"]
    except Exception:
        pass
    return ""


class Handler(BaseHTTPRequestHandler):
    provider = None  # 由 serve() 注入；None 时搜索时延迟探测
    # 审计四审安全🟡（2026-08-11）：Host 头校验——防 DNS rebinding 远程读取记忆
    ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}

    def _get_provider(self):
        if self.provider is None:
            from .embed import detect_provider
            self.provider = detect_provider()
        return self.provider

    def _check_host(self):
        """拒绝非本机 Host 头的请求（DNS rebinding 防护）"""
        host = (self.headers.get("Host") or "").split(":")[0].strip()
        if host and host not in self.ALLOWED_HOSTS:
            self._json({"error": "forbidden host"}, 403)
            return False
        return True

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._check_host():
            return
        if self.path == "/health":
            self._json({"status": "ok", "time": time.time()})
        elif self.path == "/stats":
            self._json(stats())
        elif self.path.startswith("/search"):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            query = q.get("q", [""])[0]
            ns = q.get("ns", [None])[0]
            # 审计四审安全🔵（2026-08-11）：limit/depth 上限截断（防本机放大请求 DoS）
            try:
                limit = min(int(q.get("limit", ["5"])[0]), 100)
            except ValueError:
                limit = 5
            fusion = q.get("fusion", ["0"])[0] in ("1", "true")
            task_context = q.get("task", [None])[0]
            room = q.get("room", [None])[0]
            try:
                depth = min(int(q.get("depth", ["1"])[0]), 3)
            except ValueError:
                depth = 1
            if not query:
                self._json({"error": "missing q"}, 400)
                return
            _ctx = _resolve_ctx(task_context, query, db.get_conn())
            t0 = time.time()
            if fusion:
                from .search import fusion_search
                result = fusion_search(query, namespace=ns, limit=limit,
                                       provider=self._get_provider(),
                                       task_context=_ctx or None, room=room, depth=depth)
                self._json({"query": query, "elapsed_ms": round((time.time()-t0)*1000),
                            "fusion": True, "task_context": _ctx or None,
                            "kg_expanded": result["kg_expanded"],
                            "results": result["results"]})
            else:
                from .search import search
                results = search(query, namespace=ns, limit=limit,
                                 provider=self._get_provider(), room=room)
                self._json({"query": query, "elapsed_ms": round((time.time()-t0)*1000),
                            "task_context": _ctx or None, "results": results})
        elif self.path.startswith("/set-context"):
            from urllib.parse import urlparse, parse_qs
            global _session_ctx, _session_ctx_ts
            ctx = parse_qs(urlparse(self.path).query).get("ctx", [""])[0]
            if ctx.strip():
                _session_ctx, _session_ctx_ts = ctx.strip(), time.time()
                self._json({"status": "ok", "task_context": _session_ctx})
            else:
                _session_ctx = None
                self._json({"status": "ok", "task_context": None, "cleared": True})
        elif self.path.startswith("/kg/stats"):
            from urllib.parse import urlparse, parse_qs
            ns = parse_qs(urlparse(self.path).query).get("ns", [None])[0]
            from .kg import kg_stats
            self._json(kg_stats(namespace=ns))
        elif self.path.startswith("/kg"):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            entity = q.get("entity", [""])[0]
            ns = q.get("ns", [None])[0]
            direction = q.get("direction", ["both"])[0]
            as_of = q.get("as_of", [None])[0]
            if not entity:
                self._json({"error": "missing entity"}, 400)
                return
            from .kg import kg_query
            self._json(kg_query(entity, namespace=ns, direction=direction, as_of=as_of))
        elif self.path.startswith("/wake-up"):
            # 审计 P1-8（2026-08-11 三审）：wake_up 入口补齐（原仅 CLI 可达）
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            ns = q.get("ns", [None])[0]
            try:
                mt = int(q.get("max_tokens", ["900"])[0])
            except ValueError:
                mt = 900
            from .wakeup import wake_up
            result = wake_up(namespace=ns or None, max_tokens=mt)
            self._json({"tokens": result["tokens"], "text": result["text"]})
        else:
            self._json({"error": "not found"}, 404)

    def log_message(self, fmt, *args):
        pass  # 静默访问日志


def serve(provider, host="127.0.0.1", port=8720):
    Handler.provider = provider
    try:
        srv = ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        # 审计八审🟡（2026-08-12）：端口占用给明确报错（否则双实例静默崩溃循环）
        print(f"❌ 端口 {port} 已被占用（{e}）")
        print(f"   可能已有 serve 实例在运行——先停掉旧实例再启动：")
        print(f"   · launchctl unload ~/Library/LaunchAgents/ai.eidetic.server.plist（launchd 服务）")
        print(f"   · 或 kill $(pgrep -f 'memory_server serve')（手动实例）")
        print(f"   或换端口启动: MEMORY_SERVER_PORT={port+1} ...")
        raise SystemExit(1)
    print(f"🚀 memory-server HTTP API: http://{host}:{port}")
    print(f"   GET /health | /stats | /search?q=词&limit=5")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 停止")
        srv.shutdown()
