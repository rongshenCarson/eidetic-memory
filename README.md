# Eidetic — Memory system for AI agents (RAG · Knowledge Graph · MCP Server)

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](pyproject.toml)
[![CI](https://github.com/rongshenCarson/eidetic-memory/actions/workflows/ci.yml/badge.svg)](https://github.com/rongshenCarson/eidetic-memory/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/rongshenCarson/eidetic-memory)](https://github.com/rongshenCarson/eidetic-memory/releases)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

> *Your second brain, engineered.* — a local-first memory server for LLM agents.

A single-process, single-database memory system for AI agents and LLM applications: **auto conversation ingest → five-layer memory (L0 raw → L1–L4 distilled) → fusion retrieval (RAG) → knowledge graph → self-healing**, deployed with one command.
Any MCP-compatible agent (OpenClaw / Claude Code / Hermes, etc.) can share the same memory store.

**Search keywords**: AI agent memory · RAG (retrieval-augmented generation) · semantic search · vector database · knowledge graph · MCP (Model Context Protocol) server · SQLite FTS5 · embeddings (bge-m3) · long-term memory · local-first · LLM tooling · Python

**Why Eidetic**:
- 🗄️ **Single process, single database** (SQLite: vectors + FTS5 + KG + raw layer) — no more memory bloat from multi-process, multi-database setups
- 🧠 **Five-layer memory architecture (L0–L4)**: L0 raw layer (full conversation history, never dropped) → L1 structured extraction → L2 scenes → L3 persona → L4 knowledge graph — fully automatic; aged content auto-archives (sink) out of the active recall zone but stays deep-retrievable, data is never deleted
- 🔍 **Three-tier recall**: auto-injected distilled essentials each turn (cheap) → on-demand deep fusion search (vector + BM25 + KG multi-hop) → archived recall for full history — right depth, right cost
- 🧭 **Memory wandering**: entity co-occurrence hallways + KG links + HTML visualization
- ⚡ **Auto indexing**: automatically builds a USearch vector index past a size threshold; never degrades
- 🔒 **Local-first**: data never leaves your machine, no cloud dependency

**Proven in long-term production use** (daily driver since 2026, evolved from the MemPalace lineage):
- 🧠 **Long-term recall**: memories from months ago are recalled with near-complete fidelity — the raw layer never drops data (conversation JSONL is the single source of truth), and fusion retrieval (vector + BM25 + KG) finds old context even after long gaps
- ⚡ **KV-cache friendly**: the injection design keeps the system-prompt prefix byte-stable (all dynamic memory content appended at the end), so once the session context stabilizes, **input-token cache hit rates reach ~99.8%** on prefix-caching providers — repeated turns cost a fraction of the tokens

**License**: [Apache 2.0](LICENSE)

---

## Installation (two steps: install the software → deploy the memory, <10 min)

> **Why two steps**: the installer installs the software with system privileges (files + dependencies), while deployment (initialization / OpenClaw integration / auto-start)
> needs to write into the user's own config directory — the permission scopes differ, so keeping them separate is safest.
> The installer/script handles step one; step two is done by an agent or manually — **delegating to an agent is recommended**.

### Step 1: Install the software (pick one of three)

**Option A: Double-click installer (macOS)**
Download [eidetic-memory-0.1.1-macos.pkg](https://github.com/rongshenCarson/eidetic-memory/releases/latest/download/eidetic-memory-0.1.1-macos.pkg) → double-click → installs to `/usr/local/share/eidetic-memory`
> ⚠️ The installer is not yet signed/notarized; macOS may warn "cannot verify developer" → right-click the installer → select "Open". (Will be signed for the official release.)
> Uninstall/upgrade: see [UPGRADING.md](UPGRADING.md)

**Option B: One-line terminal install**
```bash
bash <(curl -sL https://github.com/rongshenCarson/eidetic-memory/releases/latest/download/install.sh)
```
(auto: finds Python 3.11+ → creates venv → installs dependencies → ready)

**Option C: Agent-deployed (easiest)**
Tell your agent: "**deploy a memory system with eidetic-memory**" — the agent will clone/download,
install dependencies, initialize, connect OpenClaw, and start the service, completing steps one and two automatically.

### Step 2: Deploy the memory (recommended: delegate to an agent)

Tell your agent: "**use eidetic-memory to complete the memory system deployment/replacement**"
→ the agent follows [docs/AGENT-DEPLOY-GUIDE.md](docs/AGENT-DEPLOY-GUIDE.md):
init wizard → connect OpenClaw (MCP + auto injection) → install the daemon service → verify → (optional) import old memories

Or manually:
```bash
cd <install-dir>/eidetic-memory
.venv/bin/python -m memory_server init              # init wizard
.venv/bin/python -m memory_server openclaw-setup    # connect OpenClaw
.venv/bin/python -m memory_server install-service   # auto-start on boot
```

### Manual install (advanced, optional)

#### Prerequisites
- Python 3.11+ (matches `requires-python` in pyproject)
- Optional: Ollama (recommended, local embeddings) + bge-m3 model; automatically falls back to FTS-only if Ollama is absent (no feature break)
- Optional: llama.cpp in-process embeddings (`pip install -e .[embed]` or install llama-cpp-python manually); falls back to Ollama/FTS-only if not installed
- Optional: Docker (only needed for SearXNG search scenarios)

### macOS / Linux
```bash
# 1. Install (after install, .venv/bin/eidetic is equivalent to python -m memory_server; the latter is used below)
git clone https://github.com/rongshenCarson/eidetic-memory && cd eidetic-memory
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/pip install -e . --no-deps   # register package (required for MCP from any cwd)
.venv/bin/python -m memory_server init     # install wizard (probe/language/config/models/self-check)

# 2. Connect OpenClaw (MCP tools + automatic injection of distilled memories each turn)
.venv/bin/python -m memory_server openclaw-setup

# 3. Automatic conversation storage (5-minute polling)
MEMORY_AGENT_INGEST=1 .venv/bin/python -m memory_server serve

# 4. Daemon service (auto-start + crash recovery; macOS=launchd / Linux=systemd)
# ⚠️ Stop the manual serve before installing the service, otherwise two instances
#    collide on the port and crash-loop:
#    Ctrl+C to stop the serve above, or kill $(pgrep -f "memory_server serve")
.venv/bin/python -m memory_server install-service
```

### Windows
```bash
# 1. Install
git clone https://github.com/rongshenCarson/eidetic-memory && cd eidetic-memory
python -m venv .venv && .venv\Scripts\pip install -r requirements.txt
.venv\Scripts\pip install -e . --no-deps   # register package (required for MCP from any cwd)
.venv\Scripts\python -m memory_server init

# 2-4. Same as above (install-service registers a Windows service via NSSM)
```

### Non-OpenClaw users (Claude Code / Hermes, etc.)
- Session start: `eidetic wake-up` (L0 identity + L1 essentials, ~900 tokens)
- Per-turn retrieval: `eidetic search "..." --fusion` (deep recall)
- Or connect via MCP: `eidetic mcp` (usable from any MCP-compatible agent)

---

## Core commands

| Command | Purpose |
|:--|:--|
| `init` | Install wizard (probe/language/config/models/self-check) |
| `serve` | Long-running service (HTTP + scheduler + watchdog) |
| `mcp` | MCP stdio service (agent integration) |
| `agent-ingest` | Conversation ingest (5-min polling, one file per day) |
| `search <q> [--fusion]` | Retrieval (basic / fusion 5-way recall) |
| `kg <entity>` | KG entity relation query |
| `hallway <entity>` | KG-linked wandering |
| `build-hallways` | Build entity co-occurrence hallways |
| `traverse <entity>` | Spatial wandering HTML visualization |
| `wake-up` | L0+L1 wake-up context |
| `extract` | LLM distillation (L1) |
| `l2-scenes` / `l3-persona` | Scene induction / persona update |
| `promote` | Core memory promotion |
| `classify` | Auto-classification (room tagging) |
| `dedup` | Semantic dedup (cross-source) |
| `compress` | AAAK structured compression |
| `backup create/list/restore` | Backup / restore |
| `export-markdown` | Export human-readable Markdown |
| `doctor` | 23-item integrity audit |
| `install-service` | Daemon service (launchd/systemd/NSSM) |
| `openclaw-setup` | Connect OpenClaw (MCP + auto injection) |
| `maintain` | Maintenance (conflicts/feedback/curated/reflect/learnings) |
| `status` | Status overview |

---

## Architecture

```
Eidetic (single process, single database)
  L0 raw/ raw layer (single source of truth: conversation JSONL + documents, never dropped)
    ↓ ingest (agent_ingest 5min, row-level idempotent)
  memory.db (SQLite derived index)
    ├── L1 extracts    structured distillation (decision/fact/episodic)
    ├── L2 scenes      scene induction
    ├── L3 persona     user portrait
    ├── L4 entities/triples  knowledge graph (entity normalized, multi-tenant)
    ├── promoted       core memories
    ├── learnings      lessons library (LRN)
    └── curated        core aggregation
    ↓ aging (90 days) → archived (sunk out of active recall, still deep-retrievable)
    ↓ daily export
  memory_export/ (distilled core memories → injected into OpenClaw Active Memory every turn)

OpenClaw Active Memory (framework injection channel; only distilled exports are injected)
  └── automatic injection of relevant memories every turn (low frequency, small size, high quality)

Non-OpenClaw users: wake-up (session wake) + MCP retrieval = equivalent auto injection
```

### Key design decisions
- **Raw layer as source**: a corrupted database means rebuilding the index, not losing data (backup/restore via `backup`)
- **Sinking, not deleting**: content older than 90 days is auto-archived (marked `archived`, moved out of the active recall zone) — it stays fully deep-retrievable via `--include-archived`; the raw layer keeps everything forever
- **Plan-B injection**: the raw/ layer belongs to Eidetic only; the framework receives only distilled exports (avoids index storms; measured 439k→14k chunks / 16GB→3.2GB)
- **1:1 frequencies**: fully aligned with the previous system (conversation 5min / distillation 4h / scenes 24h / persona 7d / backup 24h)
- **Auto indexing**: builds a USearch index past 50k entries, retrieval switches automatically, numpy fallback with no extra dependency
- **Multi-tenancy**: `l3-persona --per-ns` builds per-namespace personas; KG is global across namespaces

### Recall: three tiers for the right depth at the right cost

Eidetic recalls at three depths, so you pay for exactly what you need:

1. **Auto-injected (shallow, per-turn)** — at session start, `wake-up` injects L0 identity + L1 distilled essentials (~600–900 tokens, leaving 95% of context for conversation). Distilled core memories are also exported daily and auto-injected into the agent framework each turn — low frequency, small size, high quality.
2. **Manual deep recall (mid–deep)** — `eidetic search "..." --fusion` runs fusion retrieval across the full index (vector + FTS5 + knowledge graph multi-hop + time decay + task bias). Use it when you need to dig into the middle/deep layers on demand.
3. **Archived recall (on-demand)** — content older than 90 days is sunk out of the active zone but never deleted; `--include-archived` retrieves it when you need the full history.

This is the same recall philosophy as the five-layer memory model: the shallow layers keep every turn cheap and fast, while the deep layers are always one command away.

---

## Testing

```bash
.venv/bin/python -m pytest tests/ -q          # 57 unit/integration tests
.venv/bin/eidetic doctor                       # 23-item integrity audit
```

---

## Data

- Database: `data/memory.db` (SQLite)
- Raw layer: `raw/` (single source of truth; index can be rebuilt from it)
- Vector index: `data/vector_index.usearch` (auto-maintained)
- Backups: `backups/` (7 retained automatically)

---

## FAQ

**Q: Do I need Ollama?**
A: Recommended (local bge-m3 embeddings, best quality for Chinese). Without Ollama it automatically falls back to FTS-only — no feature break.

**Q: Low memory / constrained device — which embedding tier?**
A: Three tiers, see section 5 of `docs/config-reference.md`:
- Default bge-m3 F16 (1.1GB, shared via Ollama, best retrieval quality)
- Downgraded bge-m3 Q8_0 (605MB, `MEMORY_EMBED_BACKEND=llama.cpp` or `ollama create`)
- Fallback fts-only (0 model memory, automatic)
Switching tiers of the same model needs no re-embedding; switching to a different model requires a full re-embed (~2h per 100k entries).

**Q: How much memory does the whole system use?**
A: Measured on a production install (3 processes: serve + scheduler + watchdog, 100k+ chunks):
- fts-only mode: ~0 model memory, runs comfortably on 2GB devices (CPU-only, no Ollama)
- Ollama bge-m3 F16: ~1.1GB total RSS (measured)
- bge-m3 Q8_0 (605MB): ~700MB total RSS
- No GPU required — the USearch vector index auto-degrades to a numpy fallback on constrained devices, and embeddings fall back gracefully (llama.cpp → Ollama → fts-only)

**Q: Does it support Chinese?**
A: Yes — FTS5 trigram tokenizer for Chinese + bge-m3 Chinese vectors.

**Q: Can multiple agents share it?**
A: Yes — one database, multiple namespaces, usable from any MCP-compatible agent; per-namespace personas via `--per-ns`.

**Q: Data safety?**
A: Local-first, data never leaves your machine. Backup/restore: `eidetic backup create` / `eidetic backup restore`.

**Q: Relation to MemPalace / memory-server?**
A: Eidetic is the official name of memory-server. It fuses all capabilities of the previous memory system (KG / LRN / fusion retrieval / wandering) into a single-process, single-database rewrite.

---

## Roadmap

- [x] Data migration (chunks + KG + LRN import verified)
- [x] Full feature set (retrieval/KG/wandering/wake-up/export/backup/maintenance)
- [x] Plan-B injection architecture / USearch auto-indexing / retrieval quality benchmarks
- [x] Linux container smoke test
- [ ] Windows native verification (ARM VM)
- [ ] 30-day long-run stability observation
