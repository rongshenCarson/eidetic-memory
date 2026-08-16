#!/usr/bin/env python3
"""
memory-server 服务主入口（P1a）
================================
单进程整合：HTTP API + MCP + 调度器 + 看门狗 + doctor
"""
import os
import time
import threading
import logging

from . import db
from .embed import detect_provider
from .scheduler import Scheduler, Task
from .watchdog import Watchdog

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
# 审计八审🔵（2026-08-12）：文件日志轮转（年 ~50MB 增长，5MB×5 份）——
# 免 sudo newsyslog，纯 python 侧实现，跨平台可用
# 数据目录：跟随 MEMORY_SERVER_DB_DIR（与 vector_index/doctor 一致，2026-08-12 统一）
# 自定义目录部署下日志与 doctor 读的 err.log 同源，避免读写错位假阴性
_data_dir = os.environ.get("MEMORY_SERVER_DB_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
try:
    os.makedirs(_data_dir, exist_ok=True)
    from logging.handlers import RotatingFileHandler
    _rfh = RotatingFileHandler(
        os.path.join(_data_dir, "server.log"), maxBytes=5 * 1024 * 1024, backupCount=5,
        encoding="utf-8")
    _rfh.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(message)s", "%H:%M:%S"))
    logging.getLogger().addHandler(_rfh)
except Exception:
    pass
log = logging.getLogger("memory-server")


def default_tasks(provider):
    """默认任务集（水位线补跑）"""
    from .ingest import ingest_dir
    from .extract import detect_extractor, extract_and_store
    from .pipeline import run_l2_scenes, run_l3_persona, run_promote

    watch_dir = os.environ.get("MEMORY_WATCH_DIR")
    extractor = detect_extractor(os.environ.get("MEMORY_EXTRACT", None))
    tasks = []
    if watch_dir:
        tasks.append(Task(
            "watch_ingest", 300,  # 5 分钟
            lambda: ingest_dir(watch_dir, "default", provider),
        ))
    if os.environ.get("MEMORY_AGENT_INGEST") == "1":
        from .agent_ingest import ingest_agent_sessions
        tasks.append(Task(
            "agent_ingest", 300,  # 5 分钟（对齐 save_dialogue 5min，一比一）
            lambda: ingest_agent_sessions(provider),
        ))
    if extractor.name != "none":
        tasks.append(Task(
            "extract", 14400,  # 4 小时（对齐 auto_extract）
            lambda: run_extract(extractor),
        ))
        tasks.append(Task(
            "l2_scenes", 86400,  # 24h（对齐 PIPELINE_L2_INTERVAL_H）
            lambda: run_l2_scenes(extractor),
        ))
        tasks.append(Task(
            "l3_persona", 7 * 86400,  # 7d（对齐 PIPELINE_L3_INTERVAL_D）
            lambda: run_l3_persona(extractor),
        ))
    tasks.append(Task(
        "promote", 6 * 3600,  # 6h（对齐 promote_core_memories 节奏）
        lambda: run_promote(),
    ))
    tasks.append(Task(
        "classify", 6 * 3600,  # 6h 增量补标（审计三审 P1-5：原无调度，room 打标不闭环）
        lambda: run_classify(),
    ))
    tasks.append(Task(
        "daily_backup", 86400,  # 每日备份（#37）
        lambda: run_maintenance(),
    ))
    # 2026-08-11 R1 修复：feedback/curated 从条件注册改为默认注册（旧系统 daily_maintenance 一直在跑）
    from .maintain import detect_feedback, sync_curated, run_reflection
    tasks.append(Task(
        "feedback", 3600,  # 1h 反馈信号（审计🟡 2026-08-11 复审：默认改 None 全库扫描）
        lambda: detect_feedback(),
    ))
    tasks.append(Task(
        "curated", 12 * 3600,  # 12h 核心归集（对齐旧系统 auto_sync_curated）
        lambda: sync_curated(),
    ))
    tasks.append(Task(
        "reflect", 86400,  # 每日反思（对齐旧系统 auto_reflection；R1 修复：原不在调度）
        lambda: run_reflection(detect_extractor()),
    ))
    tasks.append(Task(
        "openclaw_index_check", 7200,  # 2h：对齐旧 prune_index（一比一）
        lambda: run_index_check(),
    ))
    tasks.append(Task(
        "daily_export", 86400,  # 每日导出核心记忆（方案B：供 Active Memory 自动注入）
        lambda: run_daily_export(),
    ))
    tasks.append(Task(
        "archive_old", 86400,  # 每日沉底（审计三审 P1-6：原纯手动，无调度）
        lambda: run_archive_old(),
    ))
    tasks.append(Task(
        "retention", 86400,  # 每日清理策略（审计三审 P1-6：原无任何删除，库只涨不缩）
        lambda: run_retention(),
    ))
    tasks.append(Task(
        "vector_index", 3600,  # 1h：超量自动建/更新向量索引（#42，开源就绪）
        lambda: run_vector_index(),
    ))
    tasks.append(Task(
        "kg_fast", 1800,  # 30min：规则确定性 KG 增量提取（R3 增强——旧系统设计 10min 实体/KG 新鲜度，
        lambda: run_kg_fast(),  # 4h 重量提炼间隔内保证 KG 对新对话及时可见）
    ))
    tasks.append(Task(
        "aaak_compress", 21600,  # 6h：AAAK 结构化压缩补全（规则零 LLM，对标旧 dialect 轨道）
        lambda: run_aaak_compress(),
    ))
    tasks.append(Task(
        "semantic_dedup", 86400,  # 24h：语义去重（相似 chunk 合并，对标旧 dedup.py 轨道；
        lambda: run_semantic_dedup(),  # 只删安全对，默认阈值 0.15 距离）
    ))
    # 审计🔴1补充（2026-08-11）：reembed 兜底任务——即使上游有偶发空嵌入，
    # 每小时自动补嵌（仅扫描 NULL embedding 行，空跑开销极小），不再依赖人工 maintain reembed
    tasks.append(Task(
        "reembed", 3600,
        lambda: run_reembed(provider),
    ))
    return tasks


def run_reembed(provider):
    """补嵌空向量 chunk（审计🔴1：常驻摄入必须有嵌入，兜底自愈）"""
    try:
        if provider is None:
            log.warning("reembed 跳过: provider 为 None")
            return
        conn = db.get_conn()
        try:
            rows = conn.execute(
                "SELECT id, text FROM chunks WHERE embedding IS NULL LIMIT 500"
            ).fetchall()
            if not rows:
                return
            from .embed import detect_provider as _dp
            # 空嵌入补齐（N4：Ollama 偶发长度不匹配时用当前 provider 重嵌）
            texts = [r["text"][:2000] for r in rows]
            B = 32
            done = 0
            for i in range(0, len(texts), B):
                batch = texts[i:i + B]
                try:
                    vecs = provider.embed(batch)
                except Exception as e:
                    log.error(f"reembed 批次嵌入失败: {e}")
                    break
                ids = [r["id"] for r in rows[i:i + B]]
                for cid, v in zip(ids, vecs):
                    if v and len(v) == (provider.dim or 0):
                        conn.execute("UPDATE chunks SET embedding=? WHERE id=?",
                                     (db.blob_encode(v), cid))
                        done += 1
            conn.commit()
            log.info(f"🔧 reembed 补嵌: {done}/{len(rows)} 条")
        finally:
            conn.close()
    except Exception as e:
        log.error(f"reembed 失败: {e}")


def run_aaak_compress():
    """AAAK 结构化压缩补全：为缺失压缩的 chunks 生成规则压缩（零 LLM）"""
    try:
        from memory_server.compress import compress_chunks
        r = compress_chunks(namespace=None, limit=500, dry_run=False)
        log.info(f"🗜️ AAAK 压缩补全: {r.get('generated', 0)} 条")
    except Exception as e:
        log.error(f"AAAK 压缩失败: {e}")


def run_classify():
    """增量自动分类（审计三审 P1-5，2026-08-11）：room 打标闭环

    原仅 post_import 跑一次 + 手动 CLI → 新摄入 chunks 的 metadata.topic 长期缺失。
    每 6h 对未打标 chunk 补标（classifier.auto_classify 幂等，已打标跳过）。
    """
    try:
        from .classifier import auto_classify
        r = auto_classify(namespace=None, limit=20000)
        log.info(f"🏷️  自动分类: 覆盖 {r.get('covered', 0)} 条")
    except Exception as e:
        log.error(f"自动分类失败: {e}")


def run_semantic_dedup():
    """语义去重（24h 低频）：相似 chunk 保留更长者，删除安全对"""
    try:
        from memory_server.dedup import dedup
        r = dedup(namespace=None, threshold=0.15, dry_run=False, limit=5000)
        log.info(f"🔍 语义去重: 扫描 {r['scanned']} / 删除 {r['removed']}")
    except Exception as e:
        log.error(f"语义去重失败: {e}")


def run_kg_fast():
    """规则确定性 KG 增量（R3）：零 LLM，对水位线后新 chunk 快速提实体/三元组入 KG"""
    from .extract import deterministic_extract
    from .kg import kg_add
    from memory_server import db
    last = db.get_watermark("kg_fast") or (time.time() - 1800)
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT id, text, namespace FROM chunks WHERE updated_at > ? ORDER BY id DESC LIMIT 200",
            (last,),
        ).fetchall()
        added = 0
        for r in rows:
            res = deterministic_extract(r["text"])
            for f in res["facts"][:10]:
                try:
                    # 审计三审 P1-4（2026-08-11）：原写死 namespace='default'（KG 实体 8074
                    # 集中在 default vs 131 在 dialogue，组织错位）→ 用 chunk 真实 ns
                    kg_add(f["subject"], f["predicate"], f["object"], namespace=r["namespace"])
                    added += 1
                except Exception:
                    pass
        db.set_watermark("kg_fast")
        log.info(f"⚡ KG 规则增量: {len(rows)} 条扫描 / {added} 三元组")
    except Exception as e:
        log.error(f"KG 规则增量失败: {e}")
    finally:
        conn.close()


def run_vector_index():
    """超量自动建索引（#42）：chunks 超阈值时构建 USearch，检索自动切换"""
    try:
        from memory_server.vector_index import ensure_index
        from memory_server import db
        conn = db.get_conn()
        try:
            n = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            ok = ensure_index(conn)
        finally:
            conn.close()
        from memory_server.vector_index import status as vi_status
        st = vi_status()
        log.info("🔍 向量索引检查: mode=%s count=%s（chunks=%s）",
                 st["mode"], st.get("count"), n)
    except Exception as e:
        log.error(f"向量索引检查失败: {e}")


def run_daily_export():
    """每日导出：export-markdown → memory_export/（低频、小量，Active Memory 注入源）

    方案B（2026-08-10）：raw/ 退出 OpenClaw 框架索引（避免双索引风暴/冗余），
    Active Memory 只注入每天导出的精炼记忆文件。
    """
    try:
        from memory_server.export_md import main as exp_main
        exp_main(["--out", "memory_export"])
        log.info("📤 每日导出完成 → memory_export/")
    except Exception as e:
        log.error(f"每日导出失败: {e}")


def run_archive_old():
    """每日沉底（审计三审 P1-6，2026-08-11）：90 天前内容标记 archived 移出活跃检索区

    原纯手动（4,465 条是手动跑的），现入调度每日自动执行。
    沉底后仍可深检索（--include-archived），不删数据。
    """
    try:
        from memory_server.archive import archive_old
        n = archive_old(days=90, dry_run=False)
        log.info(f"📦 沉底完成: {n} 个旧 chunk 标记 archived")
    except Exception as e:
        log.error(f"沉底失败: {e}")


def run_retention():
    """每日清理策略（审计三审 P1-6，2026-08-11）：生命周期闭环，库只涨不缩的收口

    策略（保守，不删活跃数据）：
      1. 过时 KG 事实清理：valid_to 已过且超过 180 天的三元组删除（时间线修正的沉淀）
      2. 沉底 raw 文件清理：metadata archived 且超过 365 天的 chunk 对应 raw 文件删除
         （沉底满一年 = 历史归档完成，raw 原文可安全清理，DB 内 text 仍在）
      3. 空嵌入兜底：调用 reembed 逻辑（serve 有 provider 时）
    所有删除都是「已过时/已沉底满期」数据，不影响活跃检索。
    """
    try:
        conn = db.get_conn()
        try:
            # 1. 过时 KG 清理（valid_to 距今 > 180 天）
            cutoff_kg = time.time() - 180 * 86400
            # valid_to 是 YYYY-MM-DD 字符串，转成时间戳比较
            kg_del = conn.execute(
                "DELETE FROM triples WHERE valid_to IS NOT NULL "
                "AND julianday(valid_to) < julianday('now', '-180 days')").rowcount
            # 2. 沉底满一年 chunk 的 raw 文件清理（只删 raw 原文，DB text 保留）
            import os as _os
            raw_del = 0
            rows = conn.execute(
                "SELECT path FROM chunks WHERE metadata LIKE '%archived%' "
                "AND updated_at < ?", (time.time() - 365 * 86400,)).fetchall()
            for r in rows:
                p = r["path"]
                if p and _os.path.isfile(p) and _os.path.abspath(p).startswith(_os.path.abspath("raw")):
                    try:
                        _os.remove(p)
                        raw_del += 1
                    except Exception:
                        pass
            conn.commit()
            log.info(f"🧹 清理策略: KG 过时 {kg_del} 条 / 沉底 raw {raw_del} 个")
        finally:
            conn.close()
        # 3. 空嵌入兜底（有 provider 时才有效）
        try:
            from .embed import detect_provider
            provider = detect_provider(prefer="ollama")
            run_reembed(provider)
        except Exception as e:
            log.warning(f"reembed 兜底跳过: {e}")
    except Exception as e:
        log.error(f"清理策略失败: {e}")


def run_index_check():
    """OpenClaw Active Memory 索引健康检查（#35）：停更检测，停更则触发重建

    对齐旧系统 prune_memory_index.py 职责（2h 周期 → 6h 与维护节奏一致）：
    索引库超过 STALE_HOURS 未更新 → 调 openclaw memory index 重建。
    """
    import subprocess
    try:
        from scripts.check_openclaw_index import check as oc_check
    except Exception:
        # P0-1 fix (2026-08-11): the original code hardcoded an absolute home path
        # (a portability flaw — it failed silently every 2h on other machines).
        # Now derived from the code's own location, valid in any environment.
        import importlib.util
        scripts_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts",
            "check_openclaw_index.py")
        if not os.path.isfile(scripts_path):
            log.warning(f"check_openclaw_index.py 不存在: {scripts_path}，跳过索引检查")
            return
        spec = importlib.util.spec_from_file_location("check_openclaw_index", scripts_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        oc_check = mod.check
    try:
        # 只检测记录（dry_run），不自行触发重建——重建由 openclaw-memory 自动完成，
        # 避免检查任务卡在重建子进程上（2026-08-10）
        ok, report = oc_check(rebuild=False, dry_run=True)
        log.info(f"🩺 Active Memory 索引检查: {'✅' if ok else '⚠️ 见报告'} | " + " | ".join(report[:3]))
    except Exception as e:
        log.error(f"索引检查失败: {e}")


def run_maintenance():
    """每日维护：备份 + 停更自愈检查（#35/#37）"""
    from .backup import backup
    try:
        r = backup()
        log.info(f"💾 每日备份完成: {r['path']} ({r['size_mb']}MB)")
    except Exception as e:
        log.error(f"备份失败: {e}")
    # 停更自愈：FTS 索引一致性检查 + 自动 rebuild（#35）
    try:
        conn = db.get_conn()
        n_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        n_fts = conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
        conn.close()
        if n_chunks != n_fts:
            log.warning(f"🔧 FTS 索引不一致（{n_chunks}/{n_fts}）→ 自动 rebuild")
            conn = db.get_conn()
            conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
            conn.commit()
            conn.close()
            log.info("✅ FTS 重建完成")
    except Exception as e:
        log.error(f"FTS 自愈检查失败: {e}")


def run_extract(extractor):
    """提炼任务：取水位线之后新增的 chunk 做 LLM 抽取（避免重复抽取）"""
    from .extract import extract_and_store
    last = db.get_watermark("extract") or (time.time() - 86400)  # 首次：24h 窗口
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT id, text, namespace FROM chunks WHERE updated_at > ? ORDER BY id DESC LIMIT 20",
            (last,),
        ).fetchall()
    finally:
        conn.close()
    total = {"decisions": 0, "facts": 0, "terms": 0}
    for r in rows[:5]:  # 每轮最多 5 条，控制成本
        res = extract_and_store(r["text"], r["namespace"], extractor, source_id=r["id"])
        if res:
            for k in total:
                total[k] += res.get(k, 0)
    log.info(f"🧠 提炼完成: {total}（窗口 {len(rows)} 条 / 处理 {min(len(rows), 5)} 条）")


def serve():
    """常驻服务：HTTP API + 调度器 + 看门狗（MCP 单独跑，见 mcp 命令）"""
    db.init_db()
    log.info("🚀 memory-server 启动")

    # 审计🟡接线（2026-08-11）：读取 init 向导写入的 config.yaml，provider/端口选择真正生效
    cfg = load_config()
    if cfg.get("provider"):
        log.info(f"⚙️ 读取配置: 嵌入后端 = {cfg['provider']}")

    # 审计🔴1修复（2026-08-11）：常驻摄入必须有嵌入 provider，
    # 否则 agent_ingest/watch_ingest 写入全空嵌入（语义检索静默失效）
    # 审计五/六审（2026-08-11）：统一走 resolve_provider（env > config > 默认链），消除四入口分裂
    try:
        from .embed import resolve_provider
        provider = resolve_provider()
    except Exception as e:
        log.warning(f"嵌入后端解析失败，回退默认链: {e}")
        provider = detect_provider(prefer=cfg.get("provider") or None)

    # 调度器（水位线补跑）
    tasks = default_tasks(provider)
    sched = Scheduler(tasks)
    sched.start()

    # 看门狗
    wd = Watchdog(interval=300)
    wd.start()

    # HTTP API（主线程，阻塞）
    from .http_api import serve as http_serve
    port = cfg.get("port") or 8720
    try:
        http_serve(provider, port=port)
    except KeyboardInterrupt:
        pass
    finally:
        sched.stop()
        wd.stop()


def load_config():
    """读取 ~/.memory-server/config.yaml（init 向导写入）——审计🟡修复（2026-08-11）：
    原来配置写完无人读（provider/端口选择不生效，误导用户）。serve 启动时读取生效。

    返回: {"provider": str|None, "port": int|None}
    """
    cfg_path = os.path.join(os.path.expanduser("~"), ".memory-server", "config.yaml")
    out = {"provider": None, "port": None}
    if not os.path.isfile(cfg_path):
        return out
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("provider:"):
                    v = line.split(":", 1)[1].strip()
                    if v in ("llama.cpp", "ollama", "fts-only"):
                        out["provider"] = v
                elif line.startswith("port:"):
                    try:
                        out["port"] = int(line.split(":", 1)[1].strip())
                    except ValueError:
                        pass
    except Exception:
        pass
    return out
