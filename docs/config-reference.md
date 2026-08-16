# Eidetic Configuration Reference (config.yaml + environment variables)

> Complete mapping of config.yaml and environment variables. Previously undocumented; users had to read the source to discover them.

## 1. config.yaml (~/.memory-server/config.yaml, written by the init wizard)

| Field | Default | Purpose | Read by |
|:--|:--|:--|:--|
| `language` | zh | Language (zh/en/mixed/multi); influences FTS tokenizer suggestion | Written by init; informational |
| `embedding.provider` | ollama | Embedding backend (llama.cpp/ollama/fts-only); parsed uniformly by serve/MCP/CLI | **service.serve() reads at startup** |
| `embedding.model` | bge-m3 | Embedding model name | Informational (currently fixed bge-m3 GGUF) |
| `embedding.dim` | 1024 | Vector dimension | Informational |
| `fts.tokenizer` | trigram | FTS tokenizer | Informational |
| `server.host` | 127.0.0.1 | HTTP listen address | Informational (default loopback is safe) |
| `server.port` | 8720 | HTTP port | **service.serve() reads at startup** |

## 2. Environment variables (runtime behavior)

| Variable | Default | Purpose | Read by |
|:--|:--|:--|:--|
| `MEMORY_SERVER_DB_DIR` | `data/` (in package) | SQLite database directory (isolate tests/multi-instance) | db.py |
| `MEMORY_SERVER_RAW_DIR` | `raw/` (in package) | Raw layer directory | ingest.py |
| `MEMORY_WATCH_DIR` | unset | When set, enables 5-min directory-watch ingest (watch_ingest task) | service.py |
| `MEMORY_AGENT_INGEST` | unset | `1` enables 5-min OpenClaw session ingest (agent_ingest task) | service.py |
| `MEMORY_EXTRACT` | unset | Distiller selection (e.g. `deepseek`/`ollama`; empty = no distillation) | service.py |
| `MEMORY_EMBED_BACKEND` | auto | Embedding backend override (llama.cpp/ollama/fts-only); takes precedence over config.yaml; legacy name MEMORY_MCP_BACKEND still works but warns | resolve_provider (all entry points) |
| `MEMORY_FTS_STRONG` | 0.3 | Fusion retrieval FTS strong-hit weight (lower = more pure-semantic ranking) | search.py |
| `MEMORY_FTS_WEAK` | 0.2 | Fusion retrieval FTS weak-hit weight | search.py |
| `MEMORY_INGEST_WINDOW_H` | 24 | Ingest window (hours); only recent messages are ingested | agent_ingest.py |
| `MEMORY_AGENT_STATE` | `~/.memory-server/agent_ingest_state.json` | Ingest checkpoint state file | agent_ingest.py |
| `MEMORY_AGENTS_DIR` | `~/.openclaw/agents` | OpenClaw agents directory (session file source) | agent_ingest.py |
| `MEMORY_HALLWAY_FILE` | `data/hallways.json` | Entity hallway file path | hallways.py |

## 3. Scheduled task periods (service.py default_tasks)

| Task | Period | Purpose |
|:--|:--|:--|
| watch_ingest | 5min | Directory-watch ingest (requires MEMORY_WATCH_DIR) |
| agent_ingest | 5min | OpenClaw session ingest (requires MEMORY_AGENT_INGEST=1) |
| reembed | 1h | Backfill empty embeddings |
| kg_fast | 30min | Rule-based deterministic KG incremental extraction |
| feedback | 1h | Feedback signal detection (negative → LRN, positive → KG) |
| vector_index | 1h | USearch index incremental maintenance |
| extract | 4h | LLM distillation (L1) |
| promote | 6h | Core memory promotion |
| classify | 6h | Room label backfill |
| aaak_compress | 6h | AAAK rule-based compression backfill |
| openclaw_index_check | 2h | Active Memory index health check |
| curated | 12h | Core aggregation sync |
| l2_scenes | 24h | L2 scene induction |
| daily_export | 24h | Daily export to memory_export/ (Active Memory injection source) |
| archive_old | 24h | Auto-archive content older than 90 days |
| retention | 24h | Retention policy: stale KG (180d) + archived raw (365d) |
| daily_backup | 24h | Backup (7 retained) |
| reflect | 24h | Daily reflection |
| l3_persona | 7d | L3 user persona |

## 4. Namespace layout

There are two legacy conversation partitions (historical). Retrieval covers the whole database, so this only matters for per-namespace filtering/statistics:

| namespace | Content | Source |
|:--|:--|:--|
| `legacy` | Legacy conversations | Imported from pre-2026-08 legacy system |
| `dialogue` | Live conversations | agent_ingest daily ingest |
| `core` / `curated` / `projects` / `personal` / `work` | Distillation / business layers | Pipelines |
| `default` | Derived layers (persona/curated/learnings, etc.) | L2/L3/promote |
| `''` (empty) | Some legacy scene rows | Written by old versions |

Convention: new data goes to `dialogue` (live) and business namespaces; derived layers (persona/aggregation/lessons) go to `default`.
Retrieval/export have full-library fallback; the dual-track layout causes no functional gap, only a statistics caveat.

## 5. Embedding model selection guide

The embedding model is the "semantic foundation" of a memory system — it determines retrieval hit rate and recall quality. Eidetic offers three tiers,
ordered by device footprint, from smallest to largest. **Switching tiers does not require re-embedding** (quantized tiers of the same model are vector-compatible):

| Tier | Model | Size/Memory | Suitable for | Install |
|:--|:--|:--|:--|:--|
| **Default (recommended)** | bge-m3 F16 | 1.1GB (shared via Ollama; not duplicated across processes) | 16GB+ RAM; best retrieval quality | `ollama pull bge-m3` |
| **Downgrade tier** | bge-m3 Q8_0 | 605MB (in-process via llama.cpp) | 8–16GB RAM, no Ollama, or want to save ~0.5GB | `pip install -e .[embed]` auto-downloads; or `ollama create` with a local GGUF |
| **Ultra-low tier (optional)** | bge-m3 Q4_K_M | 417MB | Very tight memory but still want semantic retrieval | Manually download GGUF (gpustack/bge-m3-GGUF) + `ollama create` |
| **Fallback tier** | fts-only (pure keyword) | 0 model memory | Very low-end (<8GB), no network, basic recall only | Nothing to install; automatic |

> Note: model files are not bundled with Eidetic. The wizard only recommends and wires up the backend; users pull models on demand.
> The Q4 tier has a measured quality loss (cosine 0.969 vs F16) — only for very tight memory.

**How to switch**:
- Via config: set `embedding.provider` in `~/.memory-server/config.yaml` to `ollama` / `llama.cpp` / `fts-only`
- Or environment override: `MEMORY_EMBED_BACKEND=llama.cpp` (highest priority)
- Restart serve after switching

**Cost of switching**:
- Between bge-m3 tiers: zero cost (same model, same 1024 dims, cosine 0.9995 compatible; no re-embedding)
- To a different embedding model: full re-embed (~2h per 100k entries) + benchmark re-tuning

**Rationale**: F16 is the default because it scores best on the retrieval benchmark and installs with one command; Q8_0 covers low-memory/no-Ollama environments; fts-only backs up very low-end devices. The memory savings of the "shared Ollama service" architecture (N processes save N×1.1GB) mean shrinking the model file itself is only needed for low-end users.

## 6. query_stopwords（查询重写停用词，2026-08-12 新增）

KG 实体补全时过滤的泛词/业务词。默认仅通用词（source/推理）；**业务专属词应放配置而非代码**（开源代码不含私有词）：

| 字段 | 默认 | 作用 |
|:--|:--|:--|
| `query_stopwords` | 无（仅内置 source/推理） | 逗号分隔列表，如 `query_stopwords: ["品牌A", "品牌B"]`；命中词不参与 KG 实体补全变体 |

## 技术定位：Eidetic 与「记忆推理层」前沿的对应

> 2026-08-13 补充（基于 8/10 arXiv cs.AI 论文趋势「记忆推理层假说」）

### 行业趋势
2026-08 arXiv cs.AI 论文交叉分析提出 **Memory Trinity Architecture**（记忆三层架构）：
- **L1 存储层**：向量存取（Embedding + ANN）— 已成熟
- **L2 检索层**：相关性匹配（Hybrid Search + RAG）— 当前主流
- **L3 推理层**：记忆推理整合（冲突消解 + 时序推理）— 新兴方向

核心论断：**记忆系统正从"被动的向量检索"进化为"主动的推理整合层"**。

### Eidetic 已实现 L3 推理层的部分（对照表）
| L3 能力 | Eidetic 实现 |
|:--|:--|
| **冲突消解** | KG `valid_from/valid_to` 时间线 + 冲突修正通道（kg_invalidate 显式失效旧事实，幂等 noop） |
| **时序推理** | 检索融合时间衰减 + KG 多跳（valid 时间线参与排序） |
| **主动整合** | wake_up 分层输出（L0 身份 + L1 精华）+ L2 场景归纳（l2_scenes 24h 调度） |
| **记忆淘汰** | retention 每日清理（KG 过时 180d / raw 沉底 365d）+ archive_old 沉底（90 天自动归档） |

### 对照论文
- **TEPA**（arXiv:2608.07429，Revoking Stale Memories for Conflict-Robust Language Agents）："stale memory 撤销"——与 Eidetic KG invalidate + retention 同思路
- **PsychoAgent**（arXiv:2608.07438，Affect-Sensitive Cognitive Architecture for Conflict-Aware Memory）："情感敏感 + 冲突感知记忆"——Eidetic 冲突修正已覆盖其冲突维度

### 结论
Eidetic 的 L3 实践（时间线修正/冲突消解/自动淘汰）与 2026-08 论文趋势同向，可作为开源叙事的技术定位佐证。
