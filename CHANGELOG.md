# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1] - 2026-08-13

### Fixed
- Scheduler sleep-window miss: system sleep froze threading.Timer callbacks, so 24h tasks
  (daily_export / reflect / archive_old / retention / l2_scenes / daily_backup / semantic_dedup)
  whose due point fell inside the sleep window were skipped with no catch-up.
  Fix: periodic due() tick (default 60s, env MEMORY_SCHED_TICK) + elapsed-overflow catch-up
  in _schedule_next + re-entry guard + resilient re-arm on DB errors.
  Regression tests: tests/test_sleep_recovery.py (tick catch-up / no double-run / backoff
  preserved / env override / DB-error re-arm).

## [0.1.0] - 2026-08-11

### Added
- Single-process, single-database memory system (SQLite: vectors + FTS5 + KG + raw layer)
- Automatic conversation ingest (agent_ingest, 5-min polling, one file per day, row-level idempotent)
- Four-layer distillation (L1 structured / L2 scenes / L3 persona / KG triples)
- Fusion retrieval (vector + BM25 + KG multi-hop + time decay + task bias)
- Memory wandering (hallway KG links + entity co-occurrence hallways + traverse HTML)
- USearch auto vector indexing (>50k entries auto-build, retrieval switches automatically, numpy fallback)
- Plan-B injection architecture (OpenClaw Active Memory receives only daily distilled exports; fixes index storms)
- 22-item integrity audit (doctor)
- Backup/restore (backup create/list/restore; auto-backup before restore + vector index reset)
- Retrieval quality benchmarks (30-query golden set)
- Cross-platform daemons (macOS launchd / Linux systemd / Windows NSSM)

### Migration
- 38,474 + 48,693 chunks
- KG 17,933 triples (full migration from legacy system + auto dedup)
- 81 LRN lessons

### Fixed
- Fusion retrieval ranking imbalance (vector-dominant + two-stage candidates)
- USearch empty-result fallback to numpy
- Deep pipeline spinning on empty namespaces (now library-wide)
- Conversation ingest file granularity (one file per day)
- Dedup wrongly removing same-source slices

### Infrastructure
- Apache 2.0 license
- Cross-platform verification (macOS ✅ / Linux container ✅ / Windows pending)
- Corruption recovery drill passed

## [0.0.x] - before 2026-08-09
- memory-server prototype (P0–P1 stages, internal use)
