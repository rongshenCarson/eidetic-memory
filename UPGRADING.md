# Eidetic Upgrade Guide (UPGRADING.md)

> 2026-08-11: the previous migration mechanism had no documentation, no versioning, and no failure visibility. This guide fixes that.

## Upgrade flow (standard path)

```bash
# 1. Back up the current database (important: back up before upgrading)
.venv/bin/python -m memory_server backup

# 2. Pull the new version
git pull   # or replace the install directory

# 3. Update dependencies
.venv/bin/pip install -r requirements.txt   # or pip install -e .[embed]

# 4. Run migrations (idempotent, safe to re-run)
.venv/bin/python -m memory_server init --yes   # or call db.init_db() directly to trigger _migrate

# 5. Restart the service
launchctl kickstart -k gui/$(id -u)/ai.eidetic.server   # macOS
# systemctl restart eidetic   # Linux

# 6. Verify
.venv/bin/python -m memory_server doctor   # confirm schema version 16b = expected value
```


## pkg 用户升级（双击安装路径）

> 2026-08-13: 增加 pkg 覆盖升级支持——postinstall 检测到已有服务时自动重启，双击即完成升级。

```bash
# 1.（推荐）先备份
cd /usr/local/share/eidetic-memory
.venv/bin/python -m memory_server backup

# 2. 双击新版 .pkg（如 eidetic-memory-0.1.1-macos.pkg）覆盖安装
#    - 代码/脚本自动替换为新版
#    - data/ raw/ backups/ memory_export/ 不在 pkg 内 → 用户数据自动保留，零迁移
#    - .venv 复用（依赖未变时无需重装）；editable 注册自动刷新指向新代码

# 3. 服务自动重启（postinstall 检测到已有 ai.eidetic.memory 服务时自动 kickstart）
#    若未自动重启：launchctl kickstart -k gui/$(id -u)/ai.eidetic.memory

# 4. 验证
.venv/bin/python -m memory_server doctor   # 23 项审计 + schema 版本一致
```

**升级注意事项**
- 数据保留是设计保证：pkg 构建时显式排除 `data/ raw/ backups/ memory_export/`，覆盖安装不触碰
- 依赖变化时（见 CHANGELOG）需手动补装：`.venv/bin/pip install -r requirements.txt`
- 升级重注册 editable 包时 pip 可能尝试从 PyPI 拉构建依赖（PEP 517 build isolation）——离线/慢网络下会变慢或失败，属已知限制；通常本地 pip cache 可命中。若遇卡顿，可稍后手动补跑：`cd /usr/local/share/eidetic-memory && .venv/bin/pip install -q -e . --no-deps`
- schema 迁移幂等（`init --yes` 可重跑），doctor 16b 校验版本
- 全新安装（无已有服务）不受影响：postinstall 仅在检测到 plist 时自动重启

## How migrations work

- **Versioning**: `db.SCHEMA_VERSION` (currently v2), stored in the `schema_version` table, checked by doctor item 16b.
- **Idempotent**: all migrations are `ALTER TABLE ADD COLUMN` / `CREATE INDEX IF NOT EXISTS` / data backfill — safe to re-run.
- **Failure behavior**: `_migrate` exceptions are caught and do not block startup, but doctor 16b reports a version mismatch — visible, not silent.
- **Legacy databases (no version record)**: init_db writes the version automatically; doctor shows an "unversioned (legacy)" hint.

## Migration history

| Version | Change | Migration action |
|:--|:--|:--|
| v1 | Initial schema | — |
| v2 | core_memories UNIQUE + stopword cleanup (2026-08-11) | dedup → unique index → remove junk words |

## Rollback

```bash
# Restore the last backup (restore auto-backs-up the current database first)
.venv/bin/python -m memory_server backup restore <backup-file>
```

## Troubleshooting

- **Slow/no results after upgrade**: run `doctor`, check the 22 index-drift items → `eidetic maintain reembed` to backfill empty embeddings and rebuild the index.
- **KG anomalies after upgrade**: check the 6 KG integrity items; orphan references are reported automatically.
- **FTS desync**: doctor item 5 rebuilds automatically (`--fix`).

## Uninstall (full removal)

```bash
# 1. Stop the service + remove launchd/systemd
.venv/bin/python -m memory_server install-service --action uninstall

# 2. Remove program files
rm -rf <install-dir>/eidetic-memory   # e.g. ~/eidetic-memory or /usr/local/share/eidetic-memory

# 3. Remove config and data (⚠️ memory data is unrecoverable — confirm you no longer need it)
rm -rf ~/.memory-server            # config.yaml + memory database + backups
rm -f ~/Library/LaunchAgents/ai.eidetic.memory.plist   # macOS leftover plist

# 4. If connected to OpenClaw: remove the memory-server MCP entry from ~/.openclaw/openclaw.json
```

## Upgrade

- **Source/terminal install**: `cd <install-dir> && git pull && .venv/bin/pip install -r requirements.txt && .venv/bin/python -m memory_server install-service`
  (install-service automatically cleans up the old LABEL `ai.eidetic.server` to avoid port collisions between old and new services)
- **pkg double-click install**: download the new pkg and double-click to install (overwrites old files; config and data in ~/.memory-server are untouched)
