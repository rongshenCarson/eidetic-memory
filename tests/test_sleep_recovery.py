"""睡眠跨期补跑回归测试（2026-08-13）"""
import sys, os, time, tempfile
sys.path.insert(0, "/usr/local/share/eidetic-memory")

def _fresh_db():
    from memory_server import db as _db
    _db.DB_DIR = tempfile.mkdtemp()
    _db.DB_PATH = os.path.join(_db.DB_DIR, "memory.db")
    _db.init_db()
    return _db

def test_tick_catches_sleep_miss():
    """核心场景：Timer 回调在睡眠中丢失（run 从未被调用），tick 兜底发现超期并补跑"""
    _db = _fresh_db()
    from memory_server.scheduler import Task, Scheduler

    calls = {"n": 0}
    def job():
        calls["n"] += 1

    t = Task("tick_test", 3600, job)
    # 模拟：上次成功 3h 前（1h 周期已超期）→ 相当于睡眠错过
    _db.set_watermark("tick_test", time.time() - 3 * 3600)
    assert t.due() is True, "超期应 due"

    sched = Scheduler([t], tick_interval=0.2)
    sched.start()  # 启动时应补跑超期任务
    time.sleep(0.6)
    sched.stop()
    assert calls["n"] >= 1, f"tick 应补跑, 实际 calls={calls['n']}"
    print(f"✅ tick 兜底补跑 OK, calls={calls['n']}")

def test_no_double_run():
    """防重入：_running 标志阻止 Timer 与 tick 双触发并发"""
    _db = _fresh_db()
    from memory_server.scheduler import Task

    events = {"enter": 0, "exit": 0}
    def slow():
        events["enter"] += 1
        time.sleep(0.3)
        events["exit"] += 1

    t = Task("reentry", 3600, slow)
    t._running = True  # 模拟正在运行
    t.run()
    assert events["enter"] == 0, "运行中不应再次进入"
    t._running = False
    t.stop()
    print("✅ 防重入 OK")

def test_failure_backoff_preserved():
    """原有失败退避行为不回归"""
    _db = _fresh_db()
    from memory_server.scheduler import Task

    fails = {"n": 0}
    def flaky():
        fails["n"] += 1
        raise RuntimeError("boom")
    t = Task("backoff", 3600, flaky)
    t.run()
    assert t._consecutive_failures == 1
    assert _db.get_watermark("backoff") is None  # 失败不写水位线
    t.stop()
    print("✅ 失败退避保留 OK")

test_tick_catches_sleep_miss()
test_no_double_run()
test_failure_backoff_preserved()
print("\n全部通过 ✅")

def test_tick_env_override():
    """9号审计建议1：MEMORY_SCHED_TICK 环境变量可调 tick 间隔"""
    import importlib
    old = os.environ.get("MEMORY_SCHED_TICK")
    os.environ["MEMORY_SCHED_TICK"] = "5"
    try:
        import memory_server.scheduler as sched_mod
        importlib.reload(sched_mod)
        assert sched_mod.TICK_INTERVAL == 5, f"env 覆盖失败: {sched_mod.TICK_INTERVAL}"
        # Scheduler 默认参数跟随 env
        from memory_server.scheduler import Scheduler
        s = Scheduler([])
        assert s.tick_interval == 5
        s.stop()
        print("✅ tick 环境变量覆盖 OK")
    finally:
        if old is None:
            os.environ.pop("MEMORY_SCHED_TICK", None)
        else:
            os.environ["MEMORY_SCHED_TICK"] = old
        importlib.reload(sched_mod)

def test_schedule_next_db_error_resilient():
    """9号审计建议3：_schedule_next DB 异常不应杀死排程链（应重试而非静默死亡）"""
    from memory_server import db as _db
    _db.DB_DIR = tempfile.mkdtemp()
    _db.DB_PATH = os.path.join(_db.DB_DIR, "memory.db")
    _db.init_db()
    from memory_server.scheduler import Task

    calls = {"n": 0}
    def job():
        calls["n"] += 1

    t = Task("err_rearm", 3600, job)
    _db.set_watermark("err_rearm", time.time() - 100)

    # 模拟 get_watermark 抛异常（锁/IO 抖动）
    orig = _db.get_watermark
    def boom(name):
        raise RuntimeError("simulated db lock")
    _db.get_watermark = boom
    try:
        t.run()  # run 内部 finally 调 _schedule_next → 应捕获异常并 5s 重排
    finally:
        _db.get_watermark = orig
    assert t._timer is not None, "异常后应重排 timer（不死链）"
    t.stop()
    print("✅ 排程失败重试 OK")

test_tick_env_override()
test_schedule_next_db_error_resilient()
print("新增用例通过 ✅")
