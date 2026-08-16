#!/usr/bin/env python3
"""
memory-server OpenClaw 接入包（F7，完整版 2026-08-09）
========================================================
一键接入 OpenClaw：
  1. 注册 memory-server MCP server（agent 可用 memory_search/kg_query 等工具）
  2. 可选：memorySearch extraPaths 指向 raw/（自动召回双轨：OpenClaw 每轮注入原文）
  3. 备份 openclaw.json（安全合并，不覆盖现有配置）

用法: memory-server openclaw-setup [--no-auto-recall] [--dry-run]
"""
import os
import sys
import json
import shutil
import datetime

DEFAULT_CONFIG = os.path.expanduser("~/.openclaw/openclaw.json")


def _load_config(path):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def _save_config(path, cfg):
    with open(path, "w") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")


def openclaw_setup(no_auto_recall=False, dry_run=False, config_path=None):
    """接入 OpenClaw：注册 MCP + 可选自动召回。返回报告行列表"""
    report = []
    config_path = config_path or DEFAULT_CONFIG
    if not os.path.exists(config_path):
        return [f"❌ 未找到 OpenClaw 配置: {config_path}"]
    cfg = _load_config(config_path)

    # 当前 memory-server 绝对路径
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    venv_py = os.path.join(here, ".venv", "bin", "python")
    if not os.path.exists(venv_py):
        venv_py = os.path.join(here, ".venv", "Scripts", "python.exe")
    if not os.path.exists(venv_py):
        venv_py = sys.executable

    # 1. 注册 MCP server（合并）
    mcp = cfg.setdefault("mcp", {}).setdefault("servers", {})
    entry = {"command": venv_py, "args": ["-m", "memory_server", "mcp"], "enabled": True}
    if "memory-server" in mcp:
        report.append("ℹ️  MCP memory-server 已注册（跳过）")
    else:
        mcp["memory-server"] = entry
        report.append(f"✅ 注册 MCP server: {venv_py} -m memory_server mcp")

    # 2. 自动召回（可选）：memorySearch extraPaths 指向每日精炼导出 memory_export/
    #    方案B（2026-08-10）：raw/ 退出框架索引（避免高频写入触发索引风暴 + 双索引冗余），
    #    改注入每日导出的精炼核心记忆（低频、小量、高质量）。
    if not no_auto_recall:
        export_dir = os.path.join(here, "memory_export")
        if os.path.isdir(export_dir):
            defaults = cfg.setdefault("agents", {}).setdefault("defaults", {})
            ms = defaults.setdefault("memorySearch", {})
            if not ms.get("enabled"):
                ms["enabled"] = True
            extra = ms.setdefault("extraPaths", [])
            # 清理旧方案指向 raw/ 的残留
            raw_dir = os.path.join(here, "raw")
            if raw_dir in extra:
                extra.remove(raw_dir)
            if export_dir not in extra:
                extra.append(export_dir)
                report.append(f"✅ memorySearch extraPaths → {export_dir}（每轮注入精炼核心记忆）")
            else:
                report.append("ℹ️  extraPaths 已含 memory_export/（跳过）")
        else:
            report.append("⚠️  memory_export/ 不存在（先跑 export-markdown 或等每日导出，跳过自动召回配置）")

    # 3. 备份 + 写入
    if not dry_run:
        bak = config_path + f".bak-{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}"
        shutil.copy2(config_path, bak)
        _save_config(config_path, cfg)
        report.append(f"💾 已备份: {bak}")
        report.append(f"💾 已写入: {config_path}")
    else:
        report.append("🔍 dry-run：未写入")
    return report


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(prog="eidetic openclaw-setup",
                                     description="接入 OpenClaw（MCP + 自动召回）")
    parser.add_argument("--no-auto-recall", action="store_true", help="不配置自动召回（只注册 MCP）")
    parser.add_argument("--dry-run", action="store_true", help="只预览不写入")
    args = parser.parse_args(argv)
    for line in openclaw_setup(no_auto_recall=args.no_auto_recall, dry_run=args.dry_run):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
