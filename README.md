# Eidetic — Your second brain, engineered.

A single-process, single-database memory system: **auto conversation ingest → four-layer distillation → fusion retrieval → knowledge graph → self-healing**, deployed with one command.
Any MCP-compatible agent (OpenClaw / Hermes / Claude Code, etc.) can share the same memory store.

**Why Eidetic**:
- 🗄️ **Single process, single database** (SQLite: vectors + FTS5 + KG + raw layer) — no more memory bloat from multi-process, multi-database setups
- 🔍 **Fusion retrieval**: vector + BM25 + KG multi-hop + time decay + task bias, 5-way recall
- 🧠 **Four-layer distillation**: L1 structured → L2 scenes → L3 persona → KG triples, fully automatic
- 🧭 **Memory wandering**: entity co-occurrence hallways + KG links + HTML visualization
- ⚡ **Auto indexing**: automatically builds a USearch vector index past a size threshold; never degrades
- 🔒 **Local-first**: data never leaves your machine, no cloud dependency

**License**: [Apache 2.0](LICENSE)

---

## Installation (two steps: install the software → deploy the memory, <10 min)

> **Why two steps**: the installer installs the software with system privileges (files + dependencies), while deployment (initialization / OpenClaw integration / auto-start)
> needs to write into the user's own config directory — the permission scopes differ, so keeping them separate is safest.
> The installer/script handles step one; step two is done by an agent or manually — **delegating to an agent is recommended**.

### Step 1: Install the software (pick one of three)

**Option A: Double-click installer (macOS)**
Download [eidetic-memory-0.1.0-macos.pkg](https://github.com/rongshenCarson/eidetic-memory/releases/latest/download/eidetic-memory-0.1.0-macos.pkg) → double-click → installs to `/usr/local/share/eidetic-memory`
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
git clone https://github.com/rongshenCarson/eidetic-memory && cd memory-server
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
git clone https://github.com/rongshenCarson/eidetic-memory && cd memory-server
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
| `doctor` | 22-item integrity audit |
| `install-service` | Daemon service (launchd/systemd/NSSM) |
| `openclaw-setup` | Connect OpenClaw (MCP + auto injection) |
| `maintain` | Maintenance (conflicts/feedback/curated/reflect/learnings) |
| `status` | Status overview |

---

## Architecture

```
Eidetic (single process, single database)
  raw/ raw layer (single source of truth: conversation JSONL + documents)
    ↓ ingest (agent_ingest 5min, row-level idempotent)
  memory.db (SQLite derived index)
    ├── chunks       vectors + text (95k+ entries; auto USearch index past 50k)
    ├── entities/triples  KG (17k+ triples, entity normalized)
    ├── extracts      L1 structured (decision/fact/episodic)
    ├── scenes/persona  L2 scenes / L3 persona
    ├── learnings     lessons library (LRN)
    └── curated       core aggregation
    ↓ daily export
  memory_export/ (distilled core memories → injected into OpenClaw Active Memory every turn)

OpenClaw Active Memory (framework injection channel; only distilled exports are injected)
  └── automatic injection of relevant memories every turn (low frequency, small size, high quality)

Non-OpenClaw users: wake-up (session wake) + MCP retrieval = equivalent auto injection
```

### Key design decisions
- **Raw layer as source**: a corrupted database means rebuilding the index, not losing data (backup/restore via `backup`)
- **Plan-B injection**: the raw/ layer belongs to Eidetic only; the framework receives only distilled exports (avoids index storms; measured 439k→14k chunks / 16GB→3.2GB)
- **1:1 frequencies**: fully aligned with the previous system (conversation 5min / distillation 4h / scenes 24h / persona 7d / backup 24h)
- **Auto indexing**: builds a USearch index past 50k entries, retrieval switches automatically, numpy fallback with no extra dependency
- **Multi-tenancy**: `l3-persona --per-ns` builds per-namespace personas; KG is global across namespaces

---

## Testing

```bash
.venv/bin/python -m pytest tests/ -q          # 42 unit/integration tests
.venv/bin/eidetic doctor                       # 22-item integrity audit
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

- [x] Data migration (38k + 48k chunks, KG 17,933, LRN 81)
- [x] Full feature set (retrieval/KG/wandering/wake-up/export/backup/maintenance)
- [x] Plan-B injection architecture / USearch auto-indexing / retrieval quality benchmarks
- [x] Linux container smoke test
- [ ] Windows native verification (ARM VM)
- [ ] 30-day long-run stability observation
