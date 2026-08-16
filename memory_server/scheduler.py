#!/usr/bin/env python3
"""
memory-server 调度器（P1a）
============================
进程内调度 + 水位线补跑（9号 D5 关键设计）：
- 每个任务持久化 last_successful_run 到 watermarks 表
- 进程启动后检查「水位线距现在是否超过周期」→ 超过立即补跑
- 崩溃/休眠唤醒 = 同一个无害场景（补跑）
- 幂等保证：ingest 有内容 hash 去重，补跑不翻倍
"""
import os
import time
import threading
import logging

from . import db

log = logging.getLogger("memory-server.scheduler")

# 2026-08-13（9号审计建议）：tick 间隔环境变量化——开源用户环境多样（CI/低配 VPS/容器），
# 给一个旋钮（默认 60s，与修复前一致）而不硬编码。
TICK_INTERVAL = int(os.environ.get("MEMORY_SCHED_TICK", "60"))


class Task:
    def __init__(self, name, interval_seconds, fn):
        self.name = name
        self.interval = interval_seconds
        self.fn = fn
        self._timer = None
        self._stopped = False
        self._consecutive_failures = 0  # 审计🟡修复（2026-08-11）：失败指数退避
        self._running = False  # 2026-08-13 修复：防 tick 兜底与 Timer 双触发并发

    def due(self):
        """水位线检查：是否到期/超期"""
        last = db.get_watermark(self.name)
        if last is None:
            return True  # 从未运行 → 立即跑
        return (time.time() - last) >= self.interval

    def run(self):
        if self._stopped or self._running:
            return
        self._running = True
        try:
            log.info(f"任务 {self.name} 开始")
            self.fn()
            db.set_watermark(self.name)
            self._consecutive_failures = 0
            log.info(f"任务 {self.name} 完成 ✅")
        except Exception as e:
            self._consecutive_failures += 1
            log.error(f"任务 {self.name} 失败（连续 {self._consecutive_failures} 次）: {e}")
            # 不写水位线 → 下次检查仍 due → 补跑
        finally:
            self._running = False
            self._schedule_next()

    def _schedule_next(self):
        if self._stopped:
            return
        try:
            # 审计🟡修复（2026-08-11）：失败指数退避，封顶 interval，避免失败任务 5s 热重试刷屏
            # 原逻辑：last=0（从未成功）→ next_in=max(interval-epoch, 5)=5s → 持续失败每 5s 打一次
            last = db.get_watermark(self.name) or 0
            if self._consecutive_failures > 0:
                # 指数退避：30s × 2^(failures-1)，封顶 min(interval, 3600)
                backoff = min(self.interval, 30 * (2 ** (self._consecutive_failures - 1)))
                next_in = max(backoff, 5)
            else:
                # 正常排程：距上次成功已过的时间差
                elapsed = time.time() - last
                # 2026-08-13 修复：系统睡眠时 threading.Timer 冻结，醒来后 elapsed 已超 interval
                # → 不能再等满 interval，直接补跑（否则 24h 任务会错过整个周期）
                if elapsed >= self.interval:
                    log.info(f"任务 {self.name} 睡眠跨期 → 立即补跑")
                    t = threading.Thread(target=self.run, daemon=True)
                    t.start()
                    return
                next_in = max(self.interval - elapsed, 5)
            self._timer = threading.Timer(next_in, self.run)
            self._timer.daemon = True
            self._timer.start()
        except Exception as e:
            # 2026-08-13（9号审计建议）：DB 瞬时错误（锁/IO 抖动）不能静默杀死排程链——
            # 记录日志并重试短间隔，让失效可见且可自愈（tick 兜底之外的双保险）
            log.error(f"任务 {self.name} 排程失败: {e}，5s 后重试")
            self._timer = threading.Timer(5, self.run)
            self._timer.daemon = True
            self._timer.start()

    def start(self):
        if self.due():
            # 补跑：超期立即执行（崩溃恢复/休眠唤醒场景）
            log.info(f"任务 {self.name} 超期 → 立即补跑")
            t = threading.Thread(target=self.run, daemon=True)
            t.start()
        else:
            self._schedule_next()

    def stop(self):
        self._stopped = True
        if self._timer:
            self._timer.cancel()


class Scheduler:
    def __init__(self, tasks, tick_interval=None):
        self.tasks = tasks
        self.tick_interval = tick_interval or TICK_INTERVAL
        self._tick = None
        self._stopped = False

    def start(self):
        for t in self.tasks:
            t.start()
        # 2026-08-13 修复：兜底 tick——系统睡眠时 threading.Timer 回调可能整体丢失
        # （8/13 实测：睡眠 74min，8 个 24h 任务到期点全错过且无补跑）。
        # 每 tick_interval 秒检查所有任务 due()，超期立即补跑，不依赖 Timer 回调。
        self._start_tick()

    def _start_tick(self):
        if self._stopped:
            return
        try:
            for t in self.tasks:
                try:
                    if t.due():
                        log.info(f"任务 {t.name} 超期(兜底tick) → 立即补跑")
                        th = threading.Thread(target=t.run, daemon=True)
                        th.start()
                except Exception as e:
                    log.error(f"任务 {t.name} tick 检查异常: {e}")
        finally:
            self._tick = threading.Timer(self.tick_interval, self._start_tick)
            self._tick.daemon = True
            self._tick.start()

    def stop(self):
        self._stopped = True
        if self._tick:
            self._tick.cancel()
        for t in self.tasks:
            t.stop()
