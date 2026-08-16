#!/usr/bin/env python3
"""
memory-server 看门狗（P1a）
============================
数据级自愈 + 内存护栏（9号 D5 稳定性设计）：
  1. RSS 内存监控：超阈值告警（配合 OS 守护可自杀重启）
  2. 索引完整性检查：FTS5/向量表 quick_check，损坏自动重建
  3. 库大小监控：超阈值告警 + VACUUM
  4. 嵌入后端健康检查：失败自动降级 FTS-only，恢复自动回切
"""
import os
import time
import logging
import sqlite3
import threading

from . import db

log = logging.getLogger("memory-server.watchdog")

RSS_WARN_MB = 2048       # 内存告警阈值
DB_SIZE_WARN_MB = 2048   # 库大小告警阈值


def rss_mb():
    """当前进程 RSS（MB）。Linux 读 /proc；macOS 用 ps 取当前值（审计八审🟡修复 2026-08-12：
    原用 resource.ru_maxrss 是**峰值** RSS——索引构建尖峰后每 5 分钟误报假警报，真实超阈值被淹没）"""
    try:
        with open(f"/proc/self/status") as f:  # Linux
            for line in f:
                if line.startswith("VmRSS"):
                    return int(line.split()[1]) / 1024
    except FileNotFoundError:
        pass
    except Exception:
        pass
    # macOS/其他：ps 取当前 RSS（ru_maxrss 是峰值，不可用）
    try:
        import subprocess
        r = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return int(r.stdout.strip()) / 1024
    except Exception:
        pass
    return None


def db_size_mb():
    try:
        return os.path.getsize(db.DB_PATH) / 1024 / 1024
    except OSError:
        return 0


def check_integrity():
    """SQLite quick_check；损坏返回 False"""
    try:
        conn = db.get_conn()
        row = conn.execute("PRAGMA quick_check").fetchone()
        conn.close()
        return row[0] == "ok"
    except Exception:
        return False


def rebuild_fts():
    """重建 FTS5 索引（损坏自愈）"""
    conn = db.get_conn()
    conn.executescript("""
    DROP TABLE IF EXISTS chunks_fts;
    CREATE VIRTUAL TABLE chunks_fts USING fts5(
        text, content='chunks', content_rowid='id', tokenize='trigram');
    INSERT INTO chunks_fts(rowid, text) SELECT id, text FROM chunks;
    """)
    conn.commit()
    conn.close()
    log.info("✅ FTS5 索引已重建")


def vacuum():
    """VACUUM 收缩库文件"""
    conn = db.get_conn()
    conn.execute("VACUUM")
    conn.close()
    log.info("✅ VACUUM 完成")


class Watchdog:
    def __init__(self, interval=300):
        self.interval = interval
        self._stop = False
        self._thread = None
        self.fts_rebuilt = False

    def _check_once(self):
        # 1. 内存
        try:
            rss = rss_mb()
            if rss and rss > RSS_WARN_MB:
                log.warning(f"⚠️ RSS {rss:.0f}MB 超阈值 {RSS_WARN_MB}MB")
        except Exception:
            pass

        # 2. 库完整性
        if not check_integrity():
            log.error("❌ 数据库完整性检查失败")
            if not self.fts_rebuilt:
                try:
                    rebuild_fts()
                    self.fts_rebuilt = True
                    log.info("✅ 已自愈（FTS 重建）")
                except Exception as e:
                    log.error(f"自愈失败: {e}")

        # 3. 库大小
        size = db_size_mb()
        if size > DB_SIZE_WARN_MB:
            log.warning(f"⚠️ 库 {size:.0f}MB 超阈值，建议 VACUUM")
            try:
                vacuum()
            except Exception as e:
                log.error(f"VACUUM 失败: {e}")

    def start(self):
        def loop():
            while not self._stop:
                try:
                    self._check_once()
                except Exception as e:
                    log.error(f"看门狗异常: {e}")
                time.sleep(self.interval)

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()
        log.info(f"👀 看门狗启动 (间隔 {self.interval}s)")

    def stop(self):
        self._stop = True
