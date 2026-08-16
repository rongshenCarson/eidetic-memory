#!/usr/bin/env python3
"""
memory-server 嵌入层 — 可插拔 provider（P1a）
=============================================
支持三档：llama.cpp（进程内默认）/ Ollama（可选）/ FTS-only（降级）

优先级链（自动探测）：
  1. llama.cpp 进程内（首选，Q8_0）——零外部服务
  2. Ollama 本地服务（可选后端）
  3. None（fts-only，最后保险，明确警告）
"""
import os
import sys
import time

MODEL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models")
DEFAULT_MODEL = os.path.join(MODEL_DIR, "bge-m3-Q8_0.gguf")
DIM = 1024


class EmbeddingProvider:
    """嵌入 Provider 抽象基类"""

    def __init__(self, name, dim):
        self.name = name
        self.dim = dim

    def embed(self, texts):
        """输入 list[str]，输出 list[list[float]]"""
        raise NotImplementedError

    def close(self):
        pass


class LlamaCppProvider(EmbeddingProvider):
    """llama.cpp 进程内嵌入（首选）"""

    def __init__(self, model_path=DEFAULT_MODEL, n_threads=4):
        super().__init__("llama.cpp", DIM)
        from llama_cpp import Llama
        self._llm = Llama(
            model_path=model_path,
            embedding=True,
            n_ctx=1024,  # 审计🔵修复（2026-08-11）：原 512 < 800 字符 chunk（≈530-800 tokens），长 chunk 嵌入截断
            n_threads=n_threads,
            verbose=False,
        )
        self.model_path = model_path

    def embed(self, texts):
        out = []
        for t in texts:
            emb = self._llm.create_embedding(t)["data"][0]["embedding"]
            out.append(emb)
        return out

    def close(self):
        try:
            del self._llm
        except Exception:
            pass


class OllamaProvider(EmbeddingProvider):
    """Ollama 本地服务（可选后端，Windows 一键装场景；批量嵌入比 llama.cpp 快 ~6 倍）"""

    def __init__(self, model="bge-m3", base_url="http://localhost:11434"):
        super().__init__("ollama", DIM)
        self.model = model
        self.base_url = base_url
        import urllib.parse as _up
        u = _up.urlparse(base_url)
        self._host = u.hostname or "localhost"
        self._port = u.port or 11434
        # 2026-08-10 修复：urllib 每次新建连接会被系统代理/网络扩展间歇性劫持
        # （约每 4-5 个大请求挂一个，卡在 _read_status 等响应）→ 改用持久连接复用
        # 同一连接连续 8 个大请求实测全过。超时防挂起。
        import http.client
        self._conn = http.client.HTTPConnection(self._host, self._port, timeout=30)

    def embed(self, texts):
        return self.embed_batch(texts)

    def embed_batch(self, texts, batch_size=64):
        """批量嵌入（/api/embed 支持 input 数组，实测 800字符 0.06s/条）

        2026-08-10 修复历史：Python socket 被系统代理/网络扩展（Vortex NE）按进程规则
        间歇性劫持 → 曾改 curl 子进程（--noproxy 直连）。
        2026-08-11 六审：curl 每次 spawn 进程 ≈ 1.8s/查询，全体用户继承该惩罚（本机环境问题
        写进了通用路径）。修复：http.client 持久连接为主路径（开源用户无劫持环境，毫秒级）；
        连续失败自动降级 curl --noproxy（保留本机兼容性）。
        """
        import json, sys, time
        out = []
        n_batches = (len(texts) + batch_size - 1) // batch_size
        consecutive_failures = 0
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            body = json.dumps({"model": self.model, "input": batch}).encode()
            print(f"[embed] batch {i//batch_size}/{n_batches} start", file=sys.stderr, flush=True)
            for attempt in range(3):
                try:
                    if consecutive_failures < 2:
                        embs = self._embed_http(body, len(batch))
                    else:
                        embs = self._embed_curl(body, len(batch))
                    out.extend(embs)
                    consecutive_failures = 0
                    print(f"[embed] batch {i//batch_size} done", file=sys.stderr, flush=True)
                    break
                except Exception as e:
                    consecutive_failures += 1
                    print(f"[embed] batch {i//batch_size} attempt {attempt} failed: "
                          f"{type(e).__name__}: {e}", file=sys.stderr, flush=True)
                    if attempt == 2:
                        raise
                    time.sleep(0.2 * (attempt + 1))
            # 2026-08-10: 连续 ~15 个大请求后触发系统代理/NE 劫持 → 批间降温
            # 审计六审（2026-08-11）：降温只在 curl 降级路径需要（http.client 主路径
            # 稳定无劫持，固定 1.5s 睡眠把 0.1s 请求拖成 1.6s）；http 成功路径不睡
            if consecutive_failures > 0:
                import time as _t
                _t.sleep(1.5)
        return out

    def _embed_http(self, body, expected_len):
        """http.client 持久连接主路径（审计六审 2026-08-11：开源用户毫秒级，无劫持环境）"""
        import json, http.client
        try:
            self._conn.request("POST", "/api/embed", body=body,
                               headers={"Content-Type": "application/json"})
            resp = self._conn.getresponse()
            data = json.loads(resp.read())
            embs = data.get("embeddings")
            if not embs or len(embs) != expected_len:
                raise RuntimeError(
                    f"embedding 长度不匹配: 输入 {expected_len} / 返回 {len(embs) if embs else 0}")
            return embs
        except (ConnectionError, http.client.HTTPException, OSError) as e:
            # 连接级错误：重建连接再试一次（Ollama 可能重启/连接被劫持断开）
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = http.client.HTTPConnection(self._host, self._port, timeout=30)
            raise RuntimeError(f"http 嵌入失败（已重置连接）: {e}")

    def _embed_curl(self, body, expected_len):
        """curl 子进程兜底（审计六审：本机网络扩展劫持 Python socket 时降级，--noproxy 直连）"""
        import json, subprocess
        r = subprocess.run(
            ["curl", "-s", "--noproxy", "*", "-m", "90",
             "-X", "POST", f"{self.base_url}/api/embed",
             "-H", "Content-Type: application/json",
             "--data-binary", "@-"],
            input=body, capture_output=True, timeout=100)
        if r.returncode != 0 or not r.stdout.strip():
            raise RuntimeError(f"curl rc={r.returncode}: {r.stderr[:200]}")
        data = json.loads(r.stdout)
        embs = data.get("embeddings")
        if not embs or len(embs) != expected_len:
            raise RuntimeError(
                f"embedding 长度不匹配: 输入 {expected_len} / 返回 {len(embs) if embs else 0}")
        return embs


class FtsOnlyProvider(EmbeddingProvider):
    """FTS-only 降级（最后保险，能力降级但功能不中断）"""

    def __init__(self):
        super().__init__("fts-only", 0)

    def embed(self, texts):
        return [[] for _ in texts]


def resolve_provider():
    """统一嵌入后端解析（审计五审/六审 2026-08-11：消除四入口 split-brain）。

    优先级：
      1. 环境变量 MEMORY_EMBED_BACKEND（统一命名，新）
      2. 环境变量 MEMORY_MCP_BACKEND（旧名，兼容 + 警告）
      3. config.yaml embedding.provider（init 向导写入）
      4. 默认链 detect_provider()（llama.cpp→Ollama→fts-only）
    serve/MCP/HTTP/CLI 全部走此函数 → 同一后端，无默认分裂。
    """
    import os as _os
    v = _os.environ.get("MEMORY_EMBED_BACKEND")
    if v:
        if v not in ("llama.cpp", "ollama", "fts-only"):
            print(f"⚠️ 未知 MEMORY_EMBED_BACKEND={v}，忽略", file=sys.stderr)
        else:
            return detect_provider(prefer=v)
    old = _os.environ.get("MEMORY_MCP_BACKEND")
    if old:
        print("⚠️ MEMORY_MCP_BACKEND 已弃用，请改用 MEMORY_EMBED_BACKEND", file=sys.stderr)
        if old in ("llama.cpp", "ollama", "fts-only"):
            return detect_provider(prefer=old)
    # config.yaml（init 向导写入）
    try:
        cfg_path = _os.path.join(_os.path.expanduser("~"), ".memory-server", "config.yaml")
        if _os.path.isfile(cfg_path):
            with open(cfg_path, encoding="utf-8") as f:
                for line in f:
                    if line.strip().startswith("provider:"):
                        v = line.split(":", 1)[1].strip()
                        if v in ("llama.cpp", "ollama", "fts-only"):
                            return detect_provider(prefer=v)
    except Exception:
        pass
    return detect_provider()


def detect_provider(prefer=None):
    """自动探测可用嵌入后端（安装时/启动时调用）

    prefer: None=默认链(llama.cpp→Ollama→fts-only) | "fts-only" | "ollama" | "llama.cpp"
    """
    if prefer == "fts-only":
        return FtsOnlyProvider()

    # 指定 Ollama（迁移导入大批量场景，快 ~6 倍）
    if prefer == "ollama":
        try:
            p = OllamaProvider()
            p.embed(["测试"])
            # 审计🟡修复（2026-08-11 复审）：此分支是 MCP 默认路径，stdout 会污染 JSON-RPC 流
            print("✅ 嵌入后端: Ollama 批量 (bge-m3)", file=sys.stderr)
            return p
        except Exception as e:
            print(f"⚠️ Ollama 不可用: {e}", file=sys.stderr)

    # 1. llama.cpp（首选）
    try:
        if os.path.exists(DEFAULT_MODEL):
            t0 = time.time()
            p = LlamaCppProvider()
            print(f"✅ 嵌入后端: llama.cpp 进程内 (Q8_0, 加载 {time.time()-t0:.1f}s)", file=sys.stderr)
            return p
    except Exception as e:
        print(f"⚠️ llama.cpp 加载失败: {e}", file=sys.stderr)

    # 2. Ollama
    try:
        p = OllamaProvider()
        p.embed(["测试"])
        print("✅ 嵌入后端: Ollama (bge-m3)", file=sys.stderr)
        return p
    except Exception:
        pass

    # 3. FTS-only
    # 审计🟡修复（2026-08-11 复审）：警告也走 stderr（MCP stdio 通道必须干净）
    print("⚠️ 无可用嵌入后端 → 降级 fts-only（关键词检索），安装 Ollama 可恢复完整语义能力",
          file=sys.stderr)
    return FtsOnlyProvider()
