#!/usr/bin/env python3
"""
memory-server — 单进程记忆服务器（P1a）
=========================================
用法：
  python -m memory_server init           # 初始化数据库
  python -m memory_server ingest <dir>   # 摄入目录（--ns namespace）
  python -m memory_server import --source <dir> [--ns ns]  # 迁移导入器（P1b）
  python -m memory_server export --source <dir>... --out memories.zip  # 导出记忆包（v0.3）
  python -m memory_server import-bundle --bundle memories.zip          # 导入记忆包（v0.3）
  python -m memory_server search <词>    # 检索
  python -m memory_server serve          # 启动服务（HTTP+MCP+调度+看门狗）
  python -m memory_server mcp            # 仅 MCP（stdio）
  python -m memory_server doctor         # 自检
  python -m memory_server status         # 状态
"""
import sys
import os
import json
import argparse


def main():
    parser = argparse.ArgumentParser(prog="eidetic", description="单进程记忆服务器")
    sub = parser.add_subparsers(dest="cmd")

    p_init = sub.add_parser("init", help="安装向导（零问+确认，含探测/配置/自检）")
    p_init.add_argument("--yes", action="store_true", help="非交互全默认（懒人/CI）")
    sub.add_parser("status", help="状态")
    sub.add_parser("serve", help="启动服务（HTTP+MCP+调度+看门狗）")
    sub.add_parser("mcp", help="仅 MCP stdio")
    p_doc = sub.add_parser("doctor", help="完整性审计（22 项）")
    p_doc.add_argument("--fix", action="store_true", help="自动修复可修复项")
    p_doc.add_argument("--json", action="store_true", help="JSON 输出")

    p_import = sub.add_parser("import", help="迁移导入器（旧系统原始层 → 新系统）")
    p_import.add_argument("--source", required=True, help="源目录")
    p_import.add_argument("--ns", default=None, help="目标 namespace（默认按目录名映射）")
    p_import.add_argument("--dry-run", action="store_true", help="只统计不写入")
    p_import.add_argument("--fts-only", action="store_true", help="仅关键词模式")
    p_import.add_argument("--backend", default=None, choices=["llama.cpp", "ollama", "fts-only"],
                          help="嵌入后端（默认自动；迁移导入大批量建议 ollama，快~6倍）")

    p_export = sub.add_parser("export", help="导出记忆包（旧系统原始层 → memories.zip）")
    p_export.add_argument("--source", action="append", required=True, help="源目录（可多次）")
    p_export.add_argument("--out", default="memories.zip", help="输出 zip 路径")

    p_bundle = sub.add_parser("import-bundle", help="导入记忆包（memories.zip → 新系统）")
    p_bundle.add_argument("--bundle", required=True, help="记忆包 zip 路径")
    p_bundle.add_argument("--dry-run", action="store_true", help="只统计不写入")
    p_bundle.add_argument("--fts-only", action="store_true", help="仅关键词模式")
    p_bundle.add_argument("--backend", default=None, choices=["llama.cpp", "ollama", "fts-only"],
                          help="嵌入后端（默认自动）")

    p_import_old = sub.add_parser("import-old",
                                  help="旧记忆一键导入 + 自动增强管线（格式自动识别）")
    p_import_old.add_argument("--path", action="append", default=[], help="旧记忆路径（可多次）")
    p_import_old.add_argument("--detect", action="store_true", help="只列出探测到的旧记忆位置")
    p_import_old.add_argument("--apply", action="store_true", help="执行语义去重删除")
    p_import_old.add_argument("--dry-run", action="store_true", help="只统计不写入")
    p_import_old.add_argument("--backend", default=None, choices=["llama.cpp", "ollama", "fts-only"])

    p_ingest = sub.add_parser("ingest", help="摄入目录/文件")
    p_ingest.add_argument("path")
    p_ingest.add_argument("--ns", default="default")
    p_ingest.add_argument("--fts-only", action="store_true")
    p_ingest.add_argument("--backend", default=None, choices=["llama.cpp", "ollama", "fts-only"],
                          help="嵌入后端（默认自动）")

    p_search = sub.add_parser("search", help="检索（默认基础模式；--fusion 开启 KG 融合）")
    p_search.add_argument("query")
    p_search.add_argument("--ns", default=None)
    p_search.add_argument("--limit", type=int, default=5)
    p_search.add_argument("--fts-only", action="store_true")
    p_search.add_argument("--fusion", action="store_true", help="融合检索（向量+FTS+KG 多跳+时间衰减+偏置）")
    p_search.add_argument("--task-context", default=None, help="任务上下文关键词（项目名，命中加权）")
    p_search.add_argument("--room", default=None, help="按 room 过滤（品牌推广/技术系统/...）")
    p_search.add_argument("--depth", type=int, default=1, help="KG 多跳深度（1/2）")
    p_search.add_argument("--backend", default=None, choices=["sqlite", "mempalace"],
                          help="存储后端（默认 config 或 sqlite）")

    # G2（2026-08-11）：fusion 独立子命令（原藏在 search --fusion，不易发现）
    p_fusion = sub.add_parser("fusion", help="融合检索（向量+FTS+KG 多跳+时间衰减+偏置）")
    p_fusion.add_argument("query")
    p_fusion.add_argument("--ns", default=None)
    p_fusion.add_argument("--limit", type=int, default=5)
    p_fusion.add_argument("--fts-only", action="store_true")
    p_fusion.add_argument("--task-context", default=None, help="任务上下文关键词（项目名，命中加权）")
    p_fusion.add_argument("--room", default=None, help="按 room 过滤（品牌推广/技术系统/...）")
    p_fusion.add_argument("--depth", type=int, default=1, help="KG 多跳深度（1/2）")

    p_kg = sub.add_parser("kg", help="KG 查询实体关系")
    p_kg.add_argument("entity")
    p_kg.add_argument("--ns", default=None)
    p_kg.add_argument("--direction", default="both", choices=["outgoing", "incoming", "both"])
    p_kg.add_argument("--as-of", default=None, help="时间点 YYYY-MM-DD（默认全部）")

    p_kgstats = sub.add_parser("kg-stats", help="KG 统计")
    p_traverse = sub.add_parser("traverse", help="空间漫游 UI（生成 HTML，对标 traverse）")
    p_traverse.add_argument("entity")
    p_traverse.add_argument("--depth", type=int, default=2)
    p_traverse.add_argument("--out", default="/tmp/eidetic_traverse.html")
    p_bh = sub.add_parser("build-hallways", help="构建实体共现走廊（对齐 MemPalace hallway）")
    p_bh.add_argument("--min-co", type=int, default=2)
    p_bh.add_argument("--out", default=None)
    p_hallway = sub.add_parser("hallway", help="KG 实体关联漫游（跨域，对标 hallway/tunnel）")
    p_hallway.add_argument("entity")
    p_hallway.add_argument("--depth", type=int, default=2)
    p_hallway.add_argument("--limit", type=int, default=20)
    p_hallway.add_argument("--ns", default=None)
    p_kgstats.add_argument("--ns", default=None)

    p_extract = sub.add_parser("extract", help="手动触发 LLM 提炼（测试/补抽）")
    p_extract.add_argument("--text", default=None, help="直接指定文本（不指定则抽水位线后的 chunks）")
    p_extract.add_argument("--ns", default="default")

    p_svc = sub.add_parser("install-service", help="安装/卸载/查询系统守护服务")
    p_svc.add_argument("--action", default="install", choices=["install", "uninstall", "status"])

    p_dl = sub.add_parser("download-model", help="下载嵌入模型（断点续传）")

    p_ai = sub.add_parser("agent-ingest", help="对话接入器：监听 agent session → 净化 → 摄入")
    p_ai.add_argument("--agents-dir", default=None, help="agent sessions 根目录")
    p_ai.add_argument("--window-h", type=int, default=None, help="时间窗口（小时）")
    p_ai.add_argument("--dry-run", action="store_true", help="只统计不写入")
    p_ai.add_argument("--fts-only", action="store_true", help="仅关键词模式")
    p_ai.add_argument("--ns", default="dialogue", help="浅层 namespace（默认 dialogue 共享层）")

    p_l2 = sub.add_parser("l2-scenes", help="L2 场景归纳（24h）")
    p_l2.add_argument("--ns", default=None) # 2026-08-10: None=全库（数据不在 default）
    p_l3 = sub.add_parser("l3-persona", help="L3 画像更新（7d）")
    p_l3.add_argument("--ns", default=None) # 2026-08-10: None=全库
    p_l3.add_argument("--per-ns", action="store_true", help="按 namespace 分别提炼（多租户防串扰，Y6）")
    p_promote = sub.add_parser("promote", help="核心记忆晋升（关键词频率）")
    p_promote.add_argument("--ns", default=None) # 2026-08-10: None=全库
    p_promote.add_argument("--min-freq", type=int, default=5)

    p_cls = sub.add_parser("classify", help="自动分类（room 打标）")
    p_cls.add_argument("--ns", default=None)
    p_cls.add_argument("--limit", type=int, default=200)

    p_bk = sub.add_parser("backup", help="数据库备份/恢复（子命令: create/list/restore）")
    p_bk.add_argument("backup_args", nargs="*", default=[], help="透传给 backup.py 的子命令参数")

    p_bc = sub.add_parser("backend-compare", help="双后端检索比对（sqlite vs mempalace）")
    p_bc.add_argument("queries", nargs="+")
    p_bc.add_argument("--ns", default=None)
    p_bc.add_argument("--limit", type=int, default=5)

    p_es = sub.add_parser("entity-summary", help="实体摘要（综合状态）")
    p_es.add_argument("--ns", default=None)
    p_es.add_argument("--top", type=int, default=10)
    p_es.add_argument("--dry-run", action="store_true")

    p_xmd = sub.add_parser("export-markdown", help="导出人类可读 Markdown")
    p_xmd.add_argument("--out", default="memory_export/")
    p_xmd.add_argument("--ns", default=None)

    p_wk = sub.add_parser("wake-up", help="唤醒上下文（L0 身份 + L1 精华）")
    p_wk.add_argument("--ns", default="default")
    p_wk.add_argument("--max-tokens", type=int, default=900)

    p_cmp = sub.add_parser("compress", help="AAAK 结构化压缩")
    p_cmp.add_argument("--ns", default=None)
    p_cmp.add_argument("--limit", type=int, default=500)
    p_cmp.add_argument("--text", default=None)
    p_cmp.add_argument("--dry-run", action="store_true")

    p_dd = sub.add_parser("dedup", help="语义去重（相似内容合并）")
    p_dd.add_argument("--ns", default=None)
    p_dd.add_argument("--threshold", type=float, default=0.15)
    p_dd.add_argument("--dry-run", action="store_true", help="只报告不删除（默认）")
    p_dd.add_argument("--apply", action="store_true", help="实际删除")
    p_dd.add_argument("--limit", type=int, default=5000)

    p_oc = sub.add_parser("openclaw-setup", help="接入 OpenClaw（MCP + 自动召回）")
    p_oc.add_argument("--no-auto-recall", action="store_true", help="不配置自动召回")
    p_oc.add_argument("--dry-run", action="store_true", help="只预览不写入")

    p_mt = sub.add_parser("maintain", help="维护模块（conflicts/feedback/curated/reflect/learnings）")
    mt_sub = p_mt.add_subparsers(dest="mcmd")
    mt_sub.add_parser("conflicts").add_argument("--ns", default=None)
    mt_sub.add_parser("feedback").add_argument("--ns", default=None) # 2026-08-10: None=全库
    mt_sub.add_parser("curated").add_argument("--ns", default=None) # 2026-08-10: None=全库
    mt_sub.add_parser("reflect").add_argument("--ns", default=None) # 2026-08-10: None=全库
    p_lrn = mt_sub.add_parser("learnings")
    p_lrn.add_argument("--ns", default=None)
    p_lrn.add_argument("--keyword", default=None)

    args = parser.parse_args()

    from . import db

    if args.cmd == "init":
        from .installer import run_wizard
        sys.exit(run_wizard(yes=args.yes))
    elif args.cmd == "status":
        db.init_db()
        from .search import stats
        print(json.dumps(stats(), ensure_ascii=False, indent=2))
    elif args.cmd == "import":
        from .importer import import_dir
        from . import db as _db
        _db.init_db()
        src_base = os.path.basename(os.path.normpath(args.source))
        ns = args.ns or src_base
        if args.dry_run:
            provider = None
        else:
            from .embed import detect_provider, resolve_provider
            provider = resolve_provider() if not args.backend else detect_provider(prefer=args.backend or ("fts-only" if args.fts_only else None))
        print(f"📥 导入: {args.source} → namespace[{ns}]" + (" (dry-run)" if args.dry_run else ""))
        results = import_dir(args.source, ns, provider, dry_run=args.dry_run)
        print(f"\n✅ 导入完成: 新摄入 {results['ok']} / 跳过 {results['skipped']} / 失败 {len(results['failed'])}")
        print(f"📄 原始层复制: {results['raw_copied']} 文件")
        if results["failed"]:
            print("❌ 失败清单:")
            for path, err in results["failed"][:10]:
                print(f"  {path}: {err}")
            sys.exit(1)
    elif args.cmd == "export":
        from .bundle import export_bundle
        result = export_bundle(args.source, args.out)
        sys.exit(0 if result else 1)
    elif args.cmd == "import-bundle":
        from .bundle import import_bundle as run_bundle
        from . import db as _db
        _db.init_db()
        if args.dry_run:
            provider = None
        else:
            from .embed import detect_provider, resolve_provider
            provider = resolve_provider() if not args.backend else detect_provider(prefer=args.backend or ("fts-only" if args.fts_only else None))
        result = run_bundle(args.bundle, provider, dry_run=args.dry_run)
        sys.exit(0 if result and not result["failed"] and not result["corrupt"] else 1)
    elif args.cmd == "ingest":
        db.init_db()
        from .embed import detect_provider, resolve_provider
        provider = resolve_provider() if not args.backend else detect_provider(prefer=args.backend or ("fts-only" if args.fts_only else None))
        from .ingest import ingest_dir, ingest_file, ingest_jsonl
        if os.path.isdir(args.path):
            results = ingest_dir(args.path, args.ns, provider)
        elif args.path.endswith(".jsonl"):
            results = [ingest_jsonl(args.path, args.ns, provider)]
        else:
            results = [ingest_file(args.path, args.ns, provider)]
        ok = sum(1 for r in results if r["status"] == "ok")
        print(f"✅ 摄入完成: {ok}/{len(results)} 成功")
    elif args.cmd == "search":
        db.init_db()
        from .embed import detect_provider, resolve_provider
        from .backend import get_backend
        if args.backend == "mempalace":
            be = get_backend("mempalace")
            results = be.search(args.query, namespace=args.ns, limit=args.limit)
            print(f"🔍 「{args.query}」 Top{args.limit} (mempalace 后端):")
            for r in results:
                print(f"  [{r['score']:.3f}] {r['text'][:60]}")
            sys.exit(0)
        provider = detect_provider(prefer="fts-only" if args.fts_only else None)
        if args.fusion:
            from .search import fusion_search
            from .search import expand_context
            result = fusion_search(args.query, namespace=args.ns, limit=args.limit,
                                   provider=provider, embed=not args.fts_only,
                                   task_context=args.task_context, room=args.room,
                                   depth=args.depth)
            result["results"] = expand_context(result["results"])  # 邻居扩展
            print(f"🔍 「{args.query}」 Top{args.limit} (融合检索"
                  + (f" · KG扩展[{','.join(result['kg_expanded'])}]" if result["kg_expanded"] else "")
                  + "):")
            for r in result["results"]:
                print(f"  [{r['score']:.4f}] ({r['namespace']}) {r['text'][:60]}")
        else:
            from .search import search, expand_context
            results = search(args.query, namespace=args.ns, limit=args.limit, provider=provider,
                             embed=not args.fts_only, room=args.room)
            results = expand_context(results)  # 邻居扩展（完整上下文）
            print(f"🔍 「{args.query}」 Top{args.limit}:")
            for r in results:
                print(f"  [{r['score']:.3f}] ({r['namespace']}) {r['text'][:60]}")
    elif args.cmd == "fusion":
        db.init_db()
        from .embed import detect_provider, resolve_provider
        provider = detect_provider(prefer="fts-only" if args.fts_only else None)
        from .search import fusion_search, expand_context
        result = fusion_search(args.query, namespace=args.ns, limit=args.limit,
                               provider=provider, embed=not args.fts_only,
                               task_context=args.task_context, room=args.room,
                               depth=args.depth)
        result["results"] = expand_context(result["results"])  # 邻居扩展
        print(f"🔍 「{args.query}」 Top{args.limit} (融合检索"
              + (f" · KG扩展[{','.join(result['kg_expanded'])}]" if result["kg_expanded"] else "")
              + "):")
        for r in result["results"]:
            print(f"  [{r['score']:.4f}] ({r['namespace']}) {r['text'][:60]}")
    elif args.cmd == "kg":
        db.init_db()
        from .kg import kg_query
        result = kg_query(args.entity, namespace=args.ns, direction=args.direction, as_of=args.as_of)
        print(f"🧠 {args.entity} (type={result.get('type')}) — {result['count']} 条关系")
        for r in result["relations"]:
            arrow = "→" if r["direction"] == "outgoing" else "←"
            vf = r["valid_from"] or "?"
            vt = r["valid_to"] or "现在"
            print(f"  {args.entity} {arrow} {r['object']} [{r['predicate']}] ({vf} ~ {vt})")
    elif args.cmd == "traverse":
        db.init_db()
        from .traverse import main as traverse_main
        sys.exit(traverse_main(sys.argv[2:]))
    elif args.cmd == "build-hallways":
        db.init_db()
        from .hallways import main as bh_main
        sys.exit(bh_main(sys.argv[2:]))
    elif args.cmd == "hallway":
        db.init_db()
        from .hallway import main as hallway_main
        sys.exit(hallway_main(sys.argv[2:]))  # 跳过 eidetic hallway
    elif args.cmd == "kg-stats":
        db.init_db()
        from .kg import kg_stats
        s = kg_stats(namespace=args.ns)
        print(f"🧠 KG 统计: 实体 {s['entities']} / 三元组 {s['triples']} / 过时 {s['expired']}")
        for p in s["predicates"]:
            print(f"  {p['predicate']}: {p['count']}")
    elif args.cmd == "extract":
        db.init_db()
        from .extract import detect_extractor, extract_and_store
        extractor = detect_extractor()
        if args.text:
            res = extract_and_store(args.text, args.ns, extractor)
            print(f"🧠 提炼结果: {res}")
        else:
            from .service import run_extract
            run_extract(extractor)
            print("🧠 批量提炼完成（见上方日志）")
    elif args.cmd == "install-service":
        from .install_service import main as svc_main
        sys.exit(svc_main(["--action", args.action]))
    elif args.cmd == "download-model":
        from .downloader import main as dl_main
        sys.exit(dl_main([]))
    elif args.cmd == "agent-ingest":
        db.init_db()
        from .agent_ingest import ingest_agent_sessions
        if args.dry_run:
            provider = None
        else:
            from .embed import detect_provider, resolve_provider
            provider = detect_provider(prefer="fts-only" if args.fts_only else None)
        stats = ingest_agent_sessions(provider, agents_dir=args.agents_dir,
                                      window_h=args.window_h, dry_run=args.dry_run,
                                      namespace=args.ns)
        print(f"📥 对话接入: {stats['agents']} agents / 消息 {stats['messages']} "
              f"/ 净化过滤 {stats['purged']} / 已存 {stats['saved']} / 跳过 {stats['skipped']}")
    elif args.cmd == "l2-scenes":
        db.init_db()
        from .extract import detect_extractor
        from .pipeline import run_l2_scenes
        r = run_l2_scenes(detect_extractor(), namespace=args.ns)
        print(f"🧠 L2 场景归纳: {r if r else '无数据或已跳过'}")
    elif args.cmd == "l3-persona":
        db.init_db()
        from .extract import detect_extractor
        from .pipeline import run_l3_persona
        r = run_l3_persona(detect_extractor(), namespace=args.ns, per_ns=args.per_ns)
        print(f"🧠 L3 画像更新: {r if r else '无数据或已跳过'}")
    elif args.cmd == "promote":
        db.init_db()
        from .pipeline import run_promote
        r = run_promote(namespace=args.ns, min_freq=args.min_freq)
        print(f"🧠 核心记忆晋升: {r if r else '无高频关键词'}")
    elif args.cmd == "classify":
        db.init_db()
        from .classifier import auto_classify, room_stats
        r = auto_classify(namespace=args.ns, limit=args.limit)
        print(f"🏷️  自动分类: {r['classified']} 条新增标签")
        print("分布:", room_stats(namespace=args.ns))
    elif args.cmd == "backup":
        from .backup import main as bk_main
        sys.exit(bk_main(args.backup_args))  # 子命令: create/list/restore
    elif args.cmd == "backend-compare":
        from .backend import main_compare
        sys.exit(main_compare(args.queries))
    elif args.cmd == "entity-summary":
        from .kg import main_summary as es_main
        argv = []
        if args.ns: argv += ["--ns", args.ns]
        argv += ["--top", str(args.top)]
        if args.dry_run: argv.append("--dry-run")
        sys.exit(es_main(argv))
    elif args.cmd == "export-markdown":
        from .export_md import main as xmd_main
        argv = ["--out", args.out] + (["--ns", args.ns] if args.ns else [])
        sys.exit(xmd_main(argv))
    elif args.cmd == "import-old":
        from .importflow import main as import_old_main
        argv = []
        if args.path:
            argv += ["--path"] + args.path
        if args.detect:
            argv.append("--detect")
        if args.apply:
            argv.append("--apply")
        if args.dry_run:
            argv.append("--dry-run")
        if args.backend:
            argv += ["--backend", args.backend]
        rc = import_old_main(argv)
        sys.exit(rc)
    elif args.cmd == "wake-up":
        from .wakeup import main as wk_main
        sys.exit(wk_main(["--ns", args.ns, "--max-tokens", str(args.max_tokens)]))
    elif args.cmd == "compress":
        from .compress import main as cmp_main
        argv = []
        if args.ns: argv += ["--ns", args.ns]
        if args.limit: argv += ["--limit", str(args.limit)]
        if args.text: argv += ["--text", args.text]
        if args.dry_run: argv.append("--dry-run")
        sys.exit(cmp_main(argv))
    elif args.cmd == "dedup":
        db.init_db()
        from .dedup import dedup
        dry = not args.apply
        r = dedup(namespace=args.ns, threshold=args.threshold, dry_run=dry, limit=args.limit)
        print(f"🔍 语义去重: 扫描 {r['scanned']} / 相似对 {r['duplicates']} / "
              f"{'将删除' if dry else '已删除'} {r['removed']}"
              + ("（dry-run，--apply 执行）" if dry else ""))
        for keep, drop, dist, ns in r["pairs"][:10]:
            print(f"  保留 {keep} / 删除 {drop} (dist={dist}, ns={ns})")
    elif args.cmd == "openclaw-setup":
        from .openclaw_setup import main as oc_main
        argv = []
        if args.no_auto_recall:
            argv.append("--no-auto-recall")
        if args.dry_run:
            argv.append("--dry-run")
        sys.exit(oc_main(argv))
    elif args.cmd == "maintain":
        db.init_db()
        from .maintain import scan_conflicts, detect_feedback, sync_curated, run_reflection, list_learnings
        if args.mcmd == "conflicts":
            r = scan_conflicts(namespace=args.ns)
            print(f"⚔️  冲突扫描: {r['count']} 处")
            for c in r["conflicts"]:
                print(f"  {c['subject']} {c['predicate']}: {c['objects']} 个当前值")
        elif args.mcmd == "feedback":
            r = detect_feedback(namespace=args.ns)
            print(f"📡 反馈信号: 负 {r['negative']} / 正 {r['positive']} / 教训 {r['learnings_written']} / KG强化 {r['kg_boosted']}")
        elif args.mcmd == "curated":
            print(f"📌 curated 同步: {sync_curated(namespace=args.ns)}")
        elif args.mcmd == "reflect":
            from .extract import detect_extractor
            print(f"🪞 反思: {run_reflection(detect_extractor(), namespace=args.ns)}")
        elif args.mcmd == "learnings":
            rs = list_learnings(namespace=args.ns, keyword=args.keyword)
            print(f"📚 教训 {len(rs)} 条:")
            for r in rs:
                print(f"  [{r['code']}] {r['title'][:60]}")
    elif args.cmd == "serve":
        from .service import serve
        serve()
    elif args.cmd == "mcp":
        from .embed import resolve_provider
        from .mcp_api import main as mcp_main
        # 审计五/六审（2026-08-11）：统一 resolve_provider（MEMORY_EMBED_BACKEND > config > 默认链），
        # 不再 MCP 独走 ollama（消除 split-brain；旧 MEMORY_MCP_BACKEND 兼容自动告警）
        mcp_main(resolve_provider())
    elif args.cmd == "doctor":
        from .doctor import main as doctor_main
        sys.exit(doctor_main(argv=["--fix"] if args.fix else []))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
