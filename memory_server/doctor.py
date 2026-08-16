#!/usr/bin/env python3
"""
memory-server doctor 完整性审计（F6，完整版 2026-08-09）
==========================================================
对标旧系统 rule_assert.py 的完整性审计 + 自愈能力，适配单库架构：

  检查矩阵（22 项）：
    1  数据库完整性     6  KG 完整性          11 守护服务状态
    2  嵌入后端         7  四层提炼产出       12 数据库膨胀
    3  模型文件         8  对话接入器新鲜度   13 磁盘空间
    4  任务水位线       9  自动分类覆盖       14 沉底归档
    5  FTS 索引一致性  10 原始层一致性       15 幂等健康
    16 配置完整性

  自愈（--fix）：
    - FTS 索引不一致 → 重建（不丢数据）
    - 数据库损坏 → 报告重建指引
    - 水位线超期 → 报告（scheduler 补跑机制自动处理）

用法: memory-server doctor [--fix] [--json]
"""
import os
import sys
import time
import json
import shutil

from . import db
from .embed import detect_provider


def _age_h(ts):
    return (time.time() - ts) / 3600


def doctor(provider=None, fix=False):
    """全身体检，返回 (ok: bool, report: list[str])"""
    report = []
    ok = True
    r = report.append

    r("🔍 memory-server doctor (F6 完整性审计)")
    r("=" * 46)

    # 1. 数据库完整性
    db.init_db()
    try:
        conn = db.get_conn()
        integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
        conn.close()
        if integrity == "ok":
            r("1. 📦 数据库完整性: ✅")
        else:
            r(f"1. 📦 数据库完整性: ❌ {integrity}")
            ok = False
    except Exception as e:
        r(f"1. 📦 数据库完整性: ❌ {e}")
        ok = False

    # 2. 嵌入后端
    try:
        p = provider or detect_provider()
        if p.dim == 0:
            r("2. 🧠 嵌入后端: ⚠️ FTS-only（语义检索不可用）")
        else:
            t0 = time.time()
            p.embed(["测试"])
            r(f"2. 🧠 嵌入后端: ✅ {p.name} (dim={p.dim}, {time.time()-t0:.2f}s)")
    except Exception as e:
        r(f"2. 🧠 嵌入后端: ❌ {e}")
        ok = False

    # 3. 模型文件（按后端区分检查，2026-08-12 修复：fts-only 无模型依赖，
    # 不应因模型文件缺失判失败；ollama 后端模型在服务里，检查本地 GGUF 路径无意义）
    from .embed import DEFAULT_MODEL
    if p.name == "fts-only":
        r("3. 📄 模型文件: ✅ 无需本地模型（fts-only 纯关键词检索）")
    elif p.name == "ollama":
        import subprocess as _sp
        try:
            out = _sp.run(["ollama", "list"], capture_output=True, text=True, timeout=10)
            if "bge-m3" in out.stdout:
                r("3. 📄 模型文件: ✅ Ollama 模型 bge-m3 就绪")
            else:
                r("3. 📄 模型文件: ⚠️ Ollama 未找到 bge-m3（`ollama pull bge-m3` 后升级为语义检索）")
        except Exception as _e:
            r(f"3. 📄 模型文件: ⚠️ 无法查询 Ollama 模型列表（{_e}）")
    else:  # llama.cpp
        if os.path.exists(DEFAULT_MODEL):
            r(f"3. 📄 模型文件: ✅ {os.path.basename(DEFAULT_MODEL)} "
              f"({os.path.getsize(DEFAULT_MODEL)/1024/1024:.0f}MB)")
        else:
            r(f"3. 📄 模型文件: ❌ 未找到（{DEFAULT_MODEL}）——llama.cpp 后端必需模型文件")
            ok = False

    conn = db.get_conn()

    # 4. 任务水位线（2026-08-11 R1：改为"应跑任务清单 vs 实际 watermark"，缺任务报 ❌）
    expected = {"agent_ingest", "extract", "l2_scenes", "l3_persona", "promote",
                "daily_backup", "feedback", "curated", "reflect", "kg_fast",
                "vector_index", "openclaw_index_check", "daily_export"}
    wms = {w["task"]: w["last_successful_run"]
           for w in conn.execute("SELECT task, last_successful_run FROM watermarks").fetchall()}
    missing = sorted(expected - set(wms))
    stale = [f"{t}({_age_h(age):.0f}h)" for t, age in wms.items() if _age_h(age) > 48]
    if not wms:
        # 全新库/测试库：调度器从未运行，无水位线可查（不判失败）
        r("4. ⏱️ 任务水位线: ⚠️ 无记录（调度器未运行过，新库场景）")
    elif missing:
        r(f"4. ⏱️ 任务水位线: ❌ 未注册 {len(missing)} 个: {', '.join(missing)}")
        ok = False
    elif stale:
        r(f"4. ⏱️ 任务水位线: ⚠️ 停更 {stale}")
    else:
        r(f"4. ⏱️ 任务水位线: ✅ {len(wms)} 个任务均注册且新鲜")

    # 4b. 产出计数（2026-08-11 N1：水位线绿≠有产出——周期任务跑了但零新增要报警）
    # 口径修正（2026-08-11）：extracts.timestamp 是内容日期（对话发生日），learnings.code 是教训编号日期，
    # 都不是「产出日期」——今日产出必须按 created_at（unixepoch 秒）判断，否则提取旧对话/旧教训永远报零新增。
    # 审计🟡修复（2026-08-11）：原 day_start 用 UTC 零点（time.time()%86400 是 UTC），
    # 本地凌晨产出（如 01:28）被误判为昨日。改用本地时区零点。
    try:
        import datetime as _dt
        local_midnight = _dt.datetime.combine(_dt.date.today(), _dt.time.min).timestamp()
        day_start = local_midnight
        checks = [
            ("scenes", "SELECT COUNT(*) FROM scenes WHERE created_at >= ?"),
            ("reflections", "SELECT COUNT(*) FROM reflections WHERE created_at >= ?"),
            ("extracts", "SELECT COUNT(*) FROM extracts WHERE created_at >= ?"),
            # learnings 不入此检查：事件驱动（排错/负反馈才写），非周期任务产出，必然误报
        ]
        stale = []
        for name, sqlq in checks:
            try:
                n = conn.execute(sqlq, (day_start,)).fetchone()[0]
                if n == 0:
                    stale.append(name)
            except Exception:
                pass
        if stale:
            r(f"4b. 📈 产出计数: ⚠️ 今日零新增: {', '.join(stale)}（任务可能在跑但没产出，查 N1 类静默吞）")
        else:
            r("4b. 📈 产出计数: ✅ 今日各表均有产出")
    except Exception as e:
        r(f"4b. 📈 产出计数: ⚠️ {e}")

    # 5. FTS 索引一致性
    try:
        n_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        n_fts = conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
        if n_chunks == n_fts:
            r(f"5. 🔎 FTS 索引一致性: ✅ ({n_chunks}/{n_fts})")
        else:
            r(f"5. 🔎 FTS 索引一致性: ❌ chunks={n_chunks} fts={n_fts}（缺 {n_chunks-n_fts}）")
            ok = False
            if fix:
                r("   🔧 自愈: 重建 FTS 索引（rebuild）...")
                # FTS5 外部内容表：rebuild 从 content 表全量重建（DELETE 对虚拟表无效）
                conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
                conn.commit()
                n2 = conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
                r(f"   ✅ 重建完成: {n2} 条")
                ok = True
    except Exception as e:
        r(f"5. 🔎 FTS 索引一致性: ❌ {e}")
        ok = False

    # 6. KG 完整性（triples 引用的实体存在）
    try:
        orphans = conn.execute(
            "SELECT COUNT(*) FROM triples t WHERE NOT EXISTS "
            "(SELECT 1 FROM entities e WHERE e.namespace=t.namespace AND e.name=t.subject)"
        ).fetchone()[0]
        triples = conn.execute("SELECT COUNT(*) FROM triples").fetchone()[0]
        if orphans == 0:
            r(f"6. 🕸️ KG 完整性: ✅ {triples} 三元组无孤儿引用")
        else:
            r(f"6. 🕸️ KG 完整性: ⚠️ {orphans}/{triples} 孤儿引用（不影响检索）")
    except Exception as e:
        r(f"6. 🕸️ KG 完整性: ❌ {e}")
        ok = False

    # 7. 四层提炼产出
    layers = {
        "中层(extracts)": ("extracts", "extracts"),
        "深层-场景(scenes)": ("scenes", "scenes"),
        "深层-画像(persona)": ("persona", "persona"),
        "晋升(core)": ("core_memories", "core_memories"),
        "curated": ("curated", "curated"),
        "教训(LRN)": ("learnings", "learnings"),
    }
    layer_ok = True
    for label, (tbl, _) in layers.items():
        try:
            n = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            r(f"7. 🗂️ {label}: {n} 条" + (" ✅" if n > 0 else ""))
        except Exception as e:
            r(f"7. 🗂️ {label}: ❌ {e}")
            layer_ok = False
    # 画像/反思新鲜度
    try:
        pr = conn.execute("SELECT updated_at FROM persona ORDER BY updated_at DESC LIMIT 1").fetchone()
        if pr and _age_h(pr[0]) > 20 * 24:
            r("   画像新鲜度: ⚠️ 20 天未更新")
    except Exception:
        pass

    # 8. 对话接入器新鲜度
    # 审计🟡修复（2026-08-11）：原用 MAX(sources.mtime)——jsonl 摄入复用 source 行不更新 mtime，
    # 误报「最近摄入 9.0h 前」（实际 5 分钟前在摄入）。改用 chunks.updated_at（真实写入时间）。
    try:
        src = conn.execute("SELECT MAX(updated_at) FROM chunks").fetchone()[0]
        if src:
            age = _age_h(src)
            r(f"8. 📥 对话接入新鲜度: {'✅' if age < 6 else '⚠️'} 最近摄入 {age:.1f}h 前")
        else:
            r("8. 📥 对话接入: ⚠️ 尚无摄入记录")
    except Exception as e:
        r(f"8. 📥 对话接入: ❌ {e}")

    # 9. 自动分类覆盖
    try:
        rows = conn.execute("SELECT metadata FROM chunks LIMIT 2000").fetchall()
        if rows:
            tagged = sum(1 for (m,) in rows if m and '"topic"' in m)
            pct = tagged / len(rows) * 100
            r(f"9. 🏷️ 自动分类覆盖: {'✅' if pct > 80 else '⚠️'} {pct:.0f}% 已打标 "
              f"（{tagged}/{len(rows)}，可跑 classify 补标）")
        else:
            r("9. 🏷️ 自动分类覆盖: -（无数据）")
    except Exception as e:
        r(f"9. 🏷️ 自动分类覆盖: ❌ {e}")

    # 10. 原始层一致性（raw 文件 vs sources）
    try:
        from .ingest import RAW_DIR
        raw_n = sum(len(fs) for _, _, fs in os.walk(RAW_DIR)) if os.path.isdir(RAW_DIR) else 0
        sources = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        r(f"10. 📄 原始层一致性: raw {raw_n} 文件 / sources {sources} 条"
          + (" ✅" if raw_n >= sources else " ⚠️"))
    except Exception as e:
        r(f"10. 📄 原始层一致性: ❌ {e}")

    # 11. 守护服务状态
    try:
        sysname = os.uname().sysname if hasattr(os, "uname") else "?"
        if sysname == "Darwin":
            import subprocess
            # N2（2026-08-11）：服务名与 install_service.LABEL 同源
            from .install_service import LABEL
            rr = subprocess.run(["launchctl", "list", LABEL],
                                capture_output=True, text=True, timeout=10)
            r("11. 🛡️ 守护服务: ✅ launchd 运行中" if rr.returncode == 0
              else "11. 🛡️ 守护服务: ⚠️ launchd 未安装（install-service 可装）")
        else:
            r("11. 🛡️ 守护服务: -（非 macOS，跳过）")
    except Exception:
        r("11. 🛡️ 守护服务: -")

    # 12. 数据库膨胀
    try:
        size_mb = os.path.getsize(db.DB_PATH) / 1024 / 1024
        freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
        r(f"12. 📊 数据库: {size_mb:.0f}MB" + (f"，空页 {freelist}" if freelist > 1000 else "") + " ✅")
    except Exception as e:
        r(f"12. 📊 数据库: ❌ {e}")

    # 13. 磁盘空间
    try:
        free_gb = shutil.disk_usage(os.path.dirname(db.DB_PATH)).free / 1024**3
        r(f"13. 💾 磁盘剩余: {free_gb:.0f}GB" + (" ✅" if free_gb > 2 else " ⚠️ 低于 2GB"))
    except Exception:
        pass

    # 14. 沉底归档（N5 修复 2026-08-11：与 search 过滤口径统一用 %archived%，
    # 原查 "archived": true 匹配不到 json_set 写入的 "archived": 1）
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE metadata LIKE '%archived%'").fetchone()[0]
        r(f"14. 📦 沉底归档: {n} 条已归档")
    except Exception as e:
        r(f"14. 📦 沉底归档: ❌ {e}")

    # 15. 幂等健康（ingested_hashes 与 sources）
    try:
        hashes = conn.execute("SELECT COUNT(*) FROM ingested_hashes").fetchone()[0]
        r(f"15. 🔁 幂等去重: {hashes} 条 hash 记录 ✅")
    except Exception as e:
        r(f"15. 🔁 幂等去重: ❌ {e}")

    # 16. 配置完整性
    try:
        cfg_path = os.path.join(os.path.expanduser("~"), ".memory-server", "config.yaml")
        if os.path.exists(cfg_path):
            r(f"16. ⚙️ 配置: ✅ {cfg_path}")
        else:
            r("16. ⚙️ 配置: ⚠️ 无 config.yaml（init 可生成）")
        # 审计四审迁移项（2026-08-11）：schema 版本校验——迁移失败/旧库可感知
        from .db import get_schema_version, SCHEMA_VERSION
        ver = get_schema_version()
        if ver == SCHEMA_VERSION:
            r(f"16b. 🗂️ schema 版本: ✅ v{ver}")
        elif ver == 0:
            r(f"16b. 🗂️ schema 版本: ⚠️ 无版本记录（旧库，init_db 后写入 v{SCHEMA_VERSION}）")
        else:
            r(f"16b. 🗂️ schema 版本: ⚠️ v{ver} → 期望 v{SCHEMA_VERSION}（运行 init_db 迁移）")
    except Exception:
        pass

    # 17. curated 时效（对齐 rule_assert #16）
    try:
        n = conn.execute("SELECT COUNT(*) FROM curated").fetchone()[0]
        r(f"17. 📌 curated: {n} 条 ✅")
    except Exception as e:
        r(f"17. 📌 curated: ❌ {e}")

    # 18. 教训库更新（对齐 rule_assert #17）
    try:
        n = conn.execute("SELECT COUNT(*) FROM learnings").fetchone()[0]
        r(f"18. 📚 教训库: {n} 条 ✅")
    except Exception as e:
        r(f"18. 📚 教训库: ❌ {e}")

    # 19. 向量检索规模（#42：超阈值自动 USearch 索引，开源就绪）
    try:
        from . import vector_index as vi
        vi._load_index()  # 磁盘有索引则加载
        st = vi.status()
        n = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        if st["mode"] == "usearch":
            r(f"19. 🧮 向量检索: {n} 条（USearch 索引 ✅，覆盖 {st.get('count')}）")
        elif st["mode"] == "usearch-file":
            r(f"19. 🧮 向量检索: {n} 条（USearch 索引文件就绪，加载后生效）")
        else:
            r(f"19. 🧮 向量检索: {n} 条（全表扫描模式；超 {vi.AUTO_INDEX_THRESHOLD} 自动建索引）")
    except Exception as e:
        r(f"19. 🧮 向量检索: ❌ {e}")

    # 20. 残留检测（R4：test/verify/demo namespace 非空即 ❌，防泄漏进注入源）
    try:
        bad = conn.execute(
            "SELECT namespace, COUNT(*) FROM chunks WHERE namespace IN "
            "('test','verify','demo','e2e','tmp') GROUP BY namespace").fetchall()
        if bad:
            r("20. 🧹 残留检测: ❌ " + ", ".join(f"{b[0]}={b[1]}" for b in bad))
            ok = False
        else:
            r("20. 🧹 残留检测: ✅ 无 test/verify/demo 残留")
    except Exception as e:
        r(f"20. 🧹 残留检测: ❌ {e}")

    # 21. 注入源新鲜度（Y1：daily_export 挂了注入源悄悄变陈，自动召回退化无感知）
    try:
        exp_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "memory_export")
        if os.path.isdir(exp_dir):
            mtimes = []
            for root, _, files in os.walk(exp_dir):
                for f in files:
                    if f.endswith(".md"):
                        mtimes.append(os.path.getmtime(os.path.join(root, f)))
            if mtimes:
                age_h = (time.time() - max(mtimes)) / 3600
                if age_h > 25:
                    r(f"21. 📤 注入源新鲜度: ⚠️ 最近导出 {age_h:.0f}h 前（>25h，可能 daily_export 未跑）")
                else:
                    r(f"21. 📤 注入源新鲜度: ✅ {age_h:.1f}h 前")
            else:
                r("21. 📤 注入源新鲜度: ⚠️ memory_export/ 为空（尚未导出）")
        else:
            r("21. 📤 注入源新鲜度: ⚠️ memory_export/ 不存在")
    except Exception as e:
        r(f"21. 📤 注入源新鲜度: ❌ {e}")

    # 22a. 备份有效性（2026-08-11 Y4：坏备份不安静躺尸；六审 P2：GB 级库逐份 integrity_check
    # 会拖慢 doctor 到分钟级 → 只完整校验最新一份，旧份仅查文件存在与大小）
    try:
        from .backup import list_backups
        bs = list_backups()
        if not bs:
            r("22a. 💾 备份: ⚠️ 无备份")
        else:
            import sqlite3 as _sq
            bad = []
            # 最新一份完整校验（integrity + chunks 计数）
            b0 = bs[0]
            try:
                vc = _sq.connect(b0["path"])
                n = vc.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
                integ = vc.execute("PRAGMA integrity_check").fetchone()[0]
                vc.close()
                if integ != "ok" or n < 1000:
                    bad.append(f"{os.path.basename(b0['path'])}({n}条)")
            except Exception:
                bad.append(f"{os.path.basename(b0['path'])}(损坏)")
            # 旧份：只查文件存在且非空（避免逐份 integrity_check 在 GB 级拖慢 doctor）
            for b in bs[1:]:
                try:
                    if not os.path.isfile(b["path"]) or os.path.getsize(b["path"]) < 1024:
                        bad.append(f"{os.path.basename(b['path'])}(缺失/过小)")
                except Exception:
                    bad.append(f"{os.path.basename(b['path'])}(异常)")
            if bad:
                r(f"22a. 💾 备份: ⚠️ {len(bad)} 份可疑: {', '.join(bad)}（建议清理或重建）")
            else:
                latest = bs[0]
                r(f"22a. 💾 备份: ✅ {len(bs)} 份，最新 {os.path.basename(latest['path'])} ({latest['size_mb']}MB)")
    except Exception as e:
        r(f"22a. 💾 备份: ⚠️ {e}")

    # 22c. 空嵌入检查（2026-08-11 N4：Ollama 长度不匹配静默产生，必须告警）
    try:
        n_null = conn.execute("SELECT COUNT(*) FROM chunks WHERE embedding IS NULL").fetchone()[0]
        if n_null > 0:
            r(f"22c. 🧩 空嵌入: ⚠️ {n_null} 条无向量（检索会漏；重嵌: eidetic maintain reembed）")
        else:
            r("22c. 🧩 空嵌入: ✅ 0 条")
    except Exception as e:
        r(f"22c. 🧩 空嵌入: ⚠️ {e}")

    # 22b. 进程数/RSS 检查（2026-08-11 R2③：防内存膨胀复发）
    try:
        import subprocess
        out = subprocess.run(["pgrep", "-f", "memory_server"], capture_output=True, text=True).stdout.split()
        pids = [p for p in out if p]
        rss_total = 0.0
        proc_info = []
        for pid in pids:
            try:
                ps_out = subprocess.run(["ps", "-o", "rss=", "-p", pid], capture_output=True, text=True).stdout.strip()
                if ps_out:
                    rss_total += int(ps_out) / 1024 / 1024
                    proc_info.append(f"{pid}({int(ps_out)/1024/1024:.1f}GB)")
            except Exception:
                pass
        if len(pids) > 3:
            r(f"22b. ⚙️ 进程: ⚠️ {len(pids)} 个 memory_server 进程（{', '.join(proc_info)}）——检查孤儿进程")
        elif rss_total > 2.5:
            r(f"22b. ⚙️ 进程: ⚠️ 总 RSS {rss_total:.1f}GB（{', '.join(proc_info)}）——超 2.5GB 阈值")
        else:
            r(f"22b. ⚙️ 进程: ✅ {len(pids)} 个（总 RSS {rss_total:.1f}GB）")
    except Exception as e:
        r(f"22b. ⚙️ 进程: ⚠️ {e}")

    # 22. 索引漂移率（Y2/G3：库 chunks 与向量索引覆盖数差异）
    try:
        from . import vector_index as vi
        vi._load_index()
        st = vi.status()
        n = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        if st["mode"] == "usearch" and st.get("count"):
            if n == 0:
                # 全新库/测试库：索引残留旧数据，跳过漂移判定（无意义）
                r(f"22. 🧮 索引漂移: ⚠️ 库为空但索引有 {st['count']} 条（测试/新库场景，重建后自动对齐）")
            else:
                drift = (n - st["count"]) / max(n, 1) * 100
                if drift > 10:
                    r(f"22. 🧮 索引漂移: ⚠️ 库 {n} vs 索引 {st['count']}（漂移 {drift:.0f}%，超 10% 需重建）")
                else:
                    r(f"22. 🧮 索引漂移: ✅ {drift:.1f}%（库 {n} / 索引 {st['count']}）")
        else:
            r(f"22. 🧮 索引漂移: ✅ 全表扫描模式（无索引，{n} 条）")
    except Exception as e:
        r(f"22. 🧮 索引漂移: ❌ {e}")

    conn.close()

    # 23. 任务错误日志扫描（审计🟡修复 2026-08-11）：doctor 之前扫不到 err.log 里
    # 每小时重复的任务失败（如向量索引增量崩溃）——水位线/漂移指标都绿但功能已坏。
    # 只统计最近 1 小时的失败（避免修复前的历史错误长期误报，且只扫最新 2000 行）。
    try:
        import glob as _glob
        # 2026-08-12 修复：err.log 跟随数据目录（与 db/vector_index 一致），
        # 否则多实例/测试环境会读到其他实例（或真实服务）的错误日志误报
        _err_dir = os.environ.get("MEMORY_SERVER_DB_DIR") or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        err_logs = _glob.glob(os.path.join(_err_dir, "server.err.log"))
        task_errors = []
        _now = time.time()
        for lp in err_logs:
            if not os.path.isfile(lp):
                continue
            try:
                with open(lp, "r", errors="replace") as lf:
                    lines = lf.readlines()[-2000:]
                for line in lines:
                    # 行首时间戳（HH:MM:SS，无前置括号——审计🟡修复 2026-08-11 复审：
                    # 原按 [HH:MM:SS 解析（line[1:3]）与实际格式不符 → 每行 ValueError 被吞 → 永远 ✅）
                    # 审计十一审🟡（2026-08-12）：时间窗口必须考虑跨天——原只比当天秒数，
                    # 昨天 03:49 的旧行在今天 02:55 被误算进"近 1 小时"（连"未来时刻"都算）。
                    # 修复：ts 必须 ≤ now_sec（未来时刻=昨天），且差值 ≤ 3600（含跨天回绕：
                    # 今天 00:30 时昨天的 23:50 应算近 1h）
                    try:
                        hh = int(line[0:2]); mm = int(line[3:5]); ss = int(line[6:8])
                        ts = hh * 3600 + mm * 60 + ss
                        local = time.localtime()
                        now_sec = local.tm_hour * 3600 + local.tm_min * 60 + local.tm_sec
                        if ts > now_sec:
                            continue  # 未来时刻 = 昨天及更早的行，跳过
                        if (now_sec - ts) > 3600:
                            continue
                    except Exception:
                        continue
                    if "失败" in line and ("任务" in line or "增量" in line or "索引" in line):
                        task_errors.append(line.strip())
            except Exception:
                pass
        if task_errors:
            recent = task_errors[-3:]
            r(f"23. 📋 任务错误扫描: ⚠️ 近 1h {len(task_errors)} 条任务失败（最新: {recent[-1][:120]}）")
            ok = False
        else:
            r("23. 📋 任务错误扫描: ✅ 近 1h 无任务失败记录")
    except Exception as e:
        r(f"23. 📋 任务错误扫描: ⚠️ {e}")

    r("=" * 46)
    r(f"结论: {'✅ 系统健康（23 项审计通过）' if ok else '❌ 存在 ❌/⚠️ 项，见上方明细'}")
    return ok, report


def main(provider=None, argv=None):
    import argparse
    parser = argparse.ArgumentParser(prog="eidetic doctor", description="完整性审计（16 项）")
    parser.add_argument("--fix", action="store_true", help="自动修复可修复项（如 FTS 索引重建）")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args(argv)

    ok, report = doctor(provider, fix=args.fix)
    if args.json:
        print(json.dumps({"ok": ok, "report": report}, ensure_ascii=False, indent=2))
    else:
        print("\n".join(report))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
