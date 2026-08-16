# Agent Deployment Guide: take over Eidetic-Memory with one sentence

> Purpose: when an agent (any agent instance, e.g. OpenClaw/Claude Code) receives the instruction
> "use eidetic-memory to complete the memory system deployment/replacement",
> follow this document. Goal: **one instruction, fully automatic, no user interruption**.

## One-sentence instruction template

The user says: **"deploy/replace the memory system with eidetic-memory"** → the agent runs the flow below.

## Agent execution flow (fully automatic)

```bash
# 1. Check if already installed
if [ -d "$HOME/eidetic-memory" ]; then
    cd "$HOME/eidetic-memory" && git pull --depth 1 2>/dev/null || true
else
    # 2. Download (source mode, most portable)
    git clone --depth 1 https://github.com/rongshenCarson/eidetic-memory "$HOME/eidetic-memory" 2>/dev/null \
      || curl -sL https://github.com/rongshenCarson/eidetic-memory/releases/latest/download/eidetic-memory-0.1.1-source.tar.gz | tar xz -C "$HOME" \
      && mv "$HOME/eidetic-memory-0.1.1" "$HOME/eidetic-memory" 2>/dev/null || true
fi
cd "$HOME/eidetic-memory"

# 3. Environment + dependencies
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
.venv/bin/pip install -q -e . --no-deps  # register package; required for MCP from any cwd

# 4. Initialize (all defaults, no interruption)
# Note: ensure the embedding model is ready first, otherwise the system
# silently falls back to fts-only (retrieval quality expectations unmet)
if command -v ollama &>/dev/null; then
    ollama list 2>/dev/null | grep -q "bge-m3" || ollama pull bge-m3
fi
.venv/bin/python -m memory_server init --yes

# 5. Connect OpenClaw (MCP + auto injection)
.venv/bin/python -m memory_server openclaw-setup

# 6. Start the daemon service
.venv/bin/python -m memory_server install-service

# 7. Verify
curl -s http://127.0.0.1:8765/health
```

## Replacing old memories (if another memory system exists)

```bash
# Import old memories (auto-detects format: OpenClaw memory dir / memories.zip / plain md dir)
.venv/bin/python -m memory_server import-old --detect          # see what was detected
.venv/bin/python -m memory_server import-old --path <old-memory-path>  # import + auto enhancement pipeline

# Confirm the old system is stopped (avoid dual-write conflicts):
#  - old mempalace: no mcp process, no listening port
#  - old memory-server instance: kill the old serve process
```

## Verification checklist (deployment success criteria)

- [ ] `curl http://127.0.0.1:8765/health` returns `{"status":"ok"}`
- [ ] `.venv/bin/python -m memory_server doctor` concludes "system healthy"
- [ ] OpenClaw tool list contains `memory-server__*` (memory_search/kg_query, etc.)
- [ ] `chunks` count grows within 5 minutes of a conversation (auto memory storage active)

## Troubleshooting

| Problem | Resolution |
|:--|:--|
| Port 8765 in use | An instance is already running → `launchctl list \| grep eidetic` to check the service; no need to redeploy |
| No Python 3.11 | `brew install python@3.11` or install the official package |
| llama-cpp fails to install | Normal — falls back to Ollama/fts-only automatically, no feature break |
| Memories not stored after deployment | Confirm `install-service` ran + `doctor` item 4 watermark is fresh |
