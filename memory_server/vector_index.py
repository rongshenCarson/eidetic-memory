#!/usr/bin/env python3
"""向量索引管理器（#42 超量自动建索引，开源就绪）

背景：<20 万条 numpy 全表扫描可接受（~0.4s）；超量后线性变慢。
方案书要求"超量再考虑 USearch"——但开源考虑：用户可能一装就是百万级，
不能等出问题再修。本模块**自动**处理：

- 阈值触发：chunks > AUTO_INDEX_THRESHOLD（默认 5 万，留余量）自动构建 USearch 索引
- 检索自动切换：search 时索引存在 → USearch top-k；否则回退 numpy 全扫
- 增量维护：新 chunks 周期性 add 进索引（serve 调度内）
- 索引持久化：data/vector_index.usearch（save/load，重启不重建）

依赖：usearch（pip 可选；无依赖时自动回退 numpy，功能不降级）
"""
import os
import time
import logging

log = logging.getLogger("memory-server.vector_index")

# 数据目录：跟随 MEMORY_SERVER_DB_DIR（隔离测试/多实例）；未设置时用包内 data/
# 2026-08-12 修复：原硬编码 data/，测试库（ms_test_*）与索引分家 → doctor 漂移误报
_DATA_DIR_OVERRIDE = os.environ.get("MEMORY_SERVER_DB_DIR")
if _DATA_DIR_OVERRIDE:
    DATA_DIR = _DATA_DIR_OVERRIDE
else:
    DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
INDEX_PATH = os.path.join(DATA_DIR, "vector_index.usearch")
AUTO_INDEX_THRESHOLD = 50000   # chunks 超过此数自动建索引（numpy 全扫 5 万 ~0.2s，留余量）
MAX_SEARCH_RESULTS = 10000     # USearch 单次搜索返回上限（2026-08-12 修复：原 200 把 k 卡死，
                               # 候选不足触发 numpy 全量补满 → 97k 库每次 fusion +2.7s）

_index = None          # 内存中的索引对象
_index_count = 0       # 索引覆盖的 chunks 数
_index_built_at = 0
_incremental_fail_count = 0  # 审计🟡（2026-08-11 复审）：增量失败连续计数（模块级）


def _usearch_available():
    try:
        import usearch  # noqa
        return True
    except Exception:
        return False


def _load_index(view=False):
    """从磁盘加载索引（存在则加载）

    审计六审 P1 + 七审🔴修正（2026-08-11）：
    - 六审改 mmap（view=True）省内存，但 usearch view 索引 immutable——无法增量 add，
      且同路径 save 触发 SIGBUS（已复现）。
    - 七审修正：默认全量加载（可增量）；view 仅显式只读检索模式（调用方传 view=True）。
      增量维护（vector_index 任务）与 mmap 本质冲突，内存收益让位给正确性。
    """
    global _index, _index_count
    if not _usearch_available() or not os.path.exists(INDEX_PATH):
        return False
    try:
        from usearch.index import Index
        _index = Index.restore(INDEX_PATH, view=view)
        # usearch 2.x：size 是 int 属性（旧版是方法）
        _index_count = _index.size if isinstance(_index.size, int) else _index.size()
        return True
    except Exception as e:
        log.warning(f"向量索引加载失败（回退 numpy）: {e}")
        _index = None
        return False


def _build_index(conn, force=False):
    """全量构建 USearch 索引（chunks > 阈值 或 force）"""
    global _index, _index_count, _index_built_at
    if not _usearch_available():
        return False
    n = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    if n < AUTO_INDEX_THRESHOLD and not force:
        return False
    # 已经是索引模式且覆盖数接近 → 跳过
    if _index is not None and abs(_index_count - n) < n * 0.05 and not force:
        return False

    import numpy as np
    from usearch.index import Index
    rows = conn.execute(
        "SELECT id, embedding FROM chunks WHERE embedding IS NOT NULL").fetchall()
    if len(rows) == 0:
        return False
    # 审计八审🟡（2026-08-12）：分批 add 控制内存峰值——原一次性 np.array 实例化
    # 94k×1024 个 float（瞬态 ~4.8GB，watchdog 实证），低配机 OOM 风险；分批 5 万/批
    BATCH = 50000
    t0 = time.time()
    idx = None
    ndim = None
    for i in range(0, len(rows), BATCH):
        batch = rows[i:i + BATCH]
        ids = np.array([r["id"] for r in batch], dtype=np.uint64)
        vecs = np.array([_decode(r["embedding"]) for r in batch], dtype=np.float32)
        if idx is None:
            ndim = vecs.shape[1]
            idx = Index(ndim=ndim, metric="cos")
        idx.add(ids, vecs)
    if idx is None:
        return False
    os.makedirs(DATA_DIR, exist_ok=True)
    idx.save(INDEX_PATH)  # 全量构建：此时未 view 该文件（新索引），直接写安全
    _index, _index_count, _index_built_at = idx, len(rows), time.time()
    log.info(f"🔍 向量索引构建完成: {len(rows)} 条 ({time.time()-t0:.1f}s) → {INDEX_PATH}")
    return True


def _decode(blob):
    import struct
    n = len(blob) // 4
    return struct.unpack(f"<{n}f", blob)


def search_topk(qvec, k=50, include_ids=None):
    """用 USearch 快速 top-k；无索引返回 None（调用方回退 numpy）

    include_ids: 可选的候选 id 集合（namespace 过滤时用），None=全库
    """
    global _index
    if _index is None:
        if not _load_index():
            return None
    if _index is None:
        return None
    try:
        import numpy as np
        q = np.array(qvec, dtype=np.float32)
        # 全库搜索，取 k 的放大倍数（后续过滤）
        res = _index.search(q, min(k * 4, MAX_SEARCH_RESULTS))
        ids = [int(x) for x in res.keys]
        dists = [float(x) for x in res.distances]
        if include_ids is not None:
            pairs = [(i, d) for i, d in zip(ids, dists) if i in include_ids][:k]
        else:
            pairs = list(zip(ids, dists))[:k]
        return pairs  # [(id, distance), ...]  distance 小 = 相似
    except Exception as e:
        log.warning(f"USearch 查询失败（回退 numpy）: {e}")
        return None


def ensure_index(conn, force=False):
    """serve 调度调用：检查阈值并自动构建/更新（Y2 真增量：小差值直接 add）"""
    if not _usearch_available():
        return False
    n = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    if _index is not None and not force:
        # Y2 真增量：差值 < 5000 直接 add 新 id（USearch 原生支持），不再等全量重建
        delta = n - _index_count
        if 0 < delta < 5000:
            _incremental_add(conn)
            return True
        if delta > max(5000, n * 0.1):
            return _build_index(conn, force=True)
        return True
    return _build_index(conn, force=force)


def _index_keys(idx):
    """跨版本取 USearch 索引键集合（审计🔴2修复 2026-08-11）

    usearch 2.x：keys 是 IndexedKeys 属性（可迭代）；旧版是方法。
    size 属性已适配，keys 漏了 → 每小时增量崩溃。统一兼容处理。
    """
    try:
        k = idx.keys
        if callable(k):
            k = k()  # 旧版：方法
        return list(k)  # 新版：可迭代属性
    except Exception as e:
        # 兜底：仅当 keys 是旧版方法时才调用。⚠️ 2026-08-12 修复：
        # 新版 usearch 的 keys 是 IndexedKeys 属性（不可调用），旧代码无条件
        # idx.keys() 必抛 'IndexedKeys' object is not callable' 且掩盖原始异常。
        try:
            if callable(idx.keys):
                return list(idx.keys())
        except Exception:
            pass
        # 原始取键失败（偶发：并发修改/视图态），留档并返回空集 → 增量逻辑全量重加兜底
        log.warning(f"⚠️ _index_keys 取键失败: {type(e).__name__}: {e}（返回空集，增量将全量重加）")
        return []


def _incremental_add(conn):
    """Y2：把索引缺失的新 chunks 增量 add 进 USearch（USearch 原生支持）"""
    global _index, _index_count, _index_built_at
    if _index is None:
        if not _load_index():
            return
    import numpy as np
    try:
        # 已索引的 id 集合（从索引取，避免查库）
        indexed = set(int(x) for x in _index_keys(_index))
        rows = conn.execute(
            "SELECT id, embedding FROM chunks WHERE embedding IS NOT NULL").fetchall()
        new_ids, new_vecs = [], []
        for r in rows:
            if r["id"] not in indexed:
                v = _decode(r["embedding"])
                if len(v) == _index.ndim:
                    new_ids.append(r["id"])
                    new_vecs.append(v)
        if new_ids:
            ids = np.array(new_ids, dtype=np.uint64)
            vecs = np.array(new_vecs, dtype=np.float32)
            _index.add(ids, vecs)
            _index_count = _index.size if isinstance(_index.size, int) else _index.size()
            _index_built_at = time.time()
            # 审计七审🔴（2026-08-11）：mmap view 索引上同路径 save → SIGBUS 崩溃
            # （总线错误，try/except 接不住，serve 直接死；已 /tmp 复现）。
            # 修复：写临时文件 + os.replace 原子换名——view 持有旧 inode 仍可读，
            # 新检索重新 view 新文件；避免对 mmap 中的文件原地写。
            tmp_path = INDEX_PATH + ".tmp"
            _index.save(tmp_path)
            os.replace(tmp_path, INDEX_PATH)
            log.info(f"🔍 向量索引增量: +{len(new_ids)} 条（共 {_index_count}）")
    except Exception as e:
        log.warning(f"向量索引增量失败（下次全量重建兜底）: {e}")
        # 审计🟡修复（2026-08-11 复审）：原 getattr(globals(), ...) 对 dict 永远返回默认值 0，
        # 计数器卡在 1，「连续失败 3 次强制重建」是死代码。改模块级变量。
        global _incremental_fail_count
        _incremental_fail_count = _incremental_fail_count + 1
        if _incremental_fail_count >= 3:
            _incremental_fail_count = 0
            log.warning("向量索引增量连续失败 3 次 → 强制全量重建")
            try:
                _build_index(conn, force=True)
            except Exception as e2:
                log.error(f"强制全量重建失败: {e2}")


def status():
    """供 doctor 检查"""
    if _index is not None:
        return {"mode": "usearch", "count": _index_count, "built_at": _index_built_at}
    if os.path.exists(INDEX_PATH):
        return {"mode": "usearch-file", "count": None}
    return {"mode": "numpy-fullscan", "count": None}
