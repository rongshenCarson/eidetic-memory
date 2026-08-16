#!/usr/bin/env python3
"""#35: OpenClaw Active Memory index health check (successor to legacy prune_memory_index.py)

Responsibilities of the legacy prune_memory_index.py (must be carried over after
stopping the old launchd job):
1. Staleness detection: index database not updated for N hours -> alert
2. Trigger rebuild (done automatically by openclaw-memory; this script only monitors/notifies)

2026-08-10 rewrite: **does not open sqlite, only reads index file mtime** —
during rebuild openclaw-memory holds an exclusive write lock and any read would
block (Python signals do not interrupt C-level blocking), so file mtime is enough
for staleness detection and never risks lock contention.

Usage: python check_openclaw_index.py   # mounted as Eidetic daily task (6h)
"""
import os
import sys
import time
import glob
import json

AGENTS_DIR = os.path.expanduser("~/.openclaw/agents")
STALE_HOURS = 6   # index file not updated for >6h is considered stale
STATE_FILE = os.path.expanduser("~/.mempalace/.prune_state.json")


def _index_files():
    """Scan all agents' index database files (+mtime, without opening them)"""
    out = []
    for d in sorted(glob.glob(os.path.join(AGENTS_DIR, "*", "agent", "openclaw-agent.sqlite"))):
        try:
            mtime = os.path.getmtime(d)
        except OSError:
            continue
        out.append((d, mtime))
    return out


def check(rebuild=False, dry_run=True):
    """Return (ok, report) — based on file mtime, no lock risk"""
    files = _index_files()
    if not files:
        return True, ["no agent index db (may not be initialized yet)"]
    report = []
    ok = True
    now = time.time()
    for f, mtime in files:
        agent = f.split("/")[-3]
        age_h = (now - mtime) / 3600
        size_mb = os.path.getsize(f) / 1024 / 1024
        if age_h > STALE_HOURS:
            report.append(f"[{agent}] ⚠️ index stale {age_h:.0f}h (> {STALE_HOURS}h) {size_mb:.0f}MB")
            ok = False
        else:
            report.append(f"[{agent}] ✅ updated {age_h:.1f}h ago, {size_mb:.0f}MB")

    # state file (for rule_assert monitoring)
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump({"last_run": now, "status": "ok" if ok else "stale",
                       "detail": " | ".join(report)}, f, ensure_ascii=False)
    except Exception:
        pass
    return ok, report


def main():
    import argparse
    ap = argparse.ArgumentParser(description="OpenClaw Active Memory index health check (mtime-based)")
    ap.add_argument("--rebuild", action="store_true", help="(kept for compatibility; rebuild is done automatically by openclaw-memory)")
    ap.add_argument("--apply", action="store_true", help="(kept for compatibility)")
    args = ap.parse_args()
    ok, report = check()
    print("\n".join(report))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
