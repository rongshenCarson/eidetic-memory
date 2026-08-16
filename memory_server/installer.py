#!/usr/bin/env python3
"""
memory-server 安装向导（方案书 v0.3 §10.5）
============================================
零问 + 确认：自动探测 → 两环确认 → 语言询问 → 推荐配置 → 落地（配置/模型/自检）

  memory-server init           # 交互向导（questionary TUI）
  memory-server init --yes     # 非交互全默认（懒人/CI）

流程（两环确认均在下载动作之前）：
  Step 0  适用范围 + 硬件门槛 + 风险说明 → 确认（第一环）
  Step 1  语言问题（直接问，不检测不预填）
  Step 2  自动探测（OS/架构/内存/GPU/磁盘/网络/旧系统痕迹）
  Step 3  设备档位 + 推荐配置 + 低配预警 → 确认（第二环）
  Step 4  旧记忆导入？（扫到痕迹才问）
  Step 5  落地：写配置 → 确认模型 → 初始化库 → 自检
"""
import os
import sys
import json
import time
import shutil
import socket
import platform
import subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory_server import db  # noqa: E402

# ---------------------------------------------------------------- 常量

CONFIG_DIR = os.path.expanduser("~/.memory-server")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.yaml")

MODEL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models")
DEFAULT_GGUF = "bge-m3-Q8_0.gguf"
GGUF_DOWNLOAD_HINT = {
    "modelscope": "https://modelscope.cn/models/gpustack/bge-m3-GGUF/resolve/master/bge-m3-Q8_0.gguf",
    "huggingface": "https://huggingface.co/gpustack/bge-m3-GGUF/resolve/main/bge-m3-Q8_0.gguf",
}

SCOPE_TEXT = """
📌 本系统适用范围
  · 个人知识库 / 对话记忆 / 轻量 RAG（<100 万条）
  · 单进程单库，本地优先，数据不出机器

🖥️ 设备基础要求
  · 推荐配置：4GB+ 内存 · 2GB+ 磁盘 · x86_64 / arm64
  · 最低配置：2GB 内存（纯 FTS 模式，不加载嵌入模型）· 1GB 磁盘
"""

LANGUAGE_CHOICES = [
    "中文为主（bge-m3，中文最强）",
    "英文为主（bge-en / nomic-embed-text，英文优化）",
    "中英混合（bge-m3，多语言覆盖）",
    "其他多语言 100+（bge-m3，法/德/西/日/韩…）",
]
LANGUAGE_KEYS = ["zh", "en", "mixed", "multi"]

# ---------------------------------------------------------------- 探测（标准库，零额外依赖）

def detect_mem_gb():
    """内存大小（GB），跨平台标准库"""
    try:
        if sys.platform == "darwin":
            out = subprocess.check_output(["sysctl", "-n", "hw.memsize"]).strip()
            return int(out) / (1024 ** 3)
        if sys.platform.startswith("linux"):
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal"):
                        return int(line.split()[1]) / 1024 / 1024
        if sys.platform.startswith("win"):
            import ctypes
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            m = MEMORYSTATUSEX(); m.dwLength = ctypes.sizeof(m)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return m.ullTotalPhys / (1024 ** 3)
    except Exception:
        pass
    return None


def detect_gpu():
    """GPU 检测：Apple Silicon 或 NVIDIA"""
    if sys.platform == "darwin" and platform.machine() == "arm64":
        return "apple-silicon"
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.check_output(["nvidia-smi", "-L"], stderr=subprocess.DEVNULL)
            return out.decode().strip().splitlines()[0] if out.strip() else "nvidia"
        except Exception:
            return "nvidia"
    return None


def detect_network():
    """网络可达性：modelscope / huggingface（2s 超时）"""
    def reachable(host):
        try:
            socket.create_connection((host, 443), timeout=2)
            return True
        except Exception:
            return False
    return {"modelscope": reachable("modelscope.cn"), "huggingface": reachable("huggingface.co")}


def detect_old_memory():
    """扫描常见旧系统痕迹（供 Step 4 询问）——增强版，复用 importflow 探测"""
    try:
        from memory_server.importflow import detect_old_memory as _detect
        return _detect()
    except Exception:
        pass
    hints = []
    candidates = [
        os.path.expanduser("~/.openclaw/workspace/memory/dialogue"),
        os.path.expanduser("~/.openclaw/workspace/memory/core"),
        os.path.expanduser("~/.openclaw/workspace/.learnings"),
        os.path.expanduser("~/.openclaw/workspace/memory"),
    ]
    for p in candidates:
        if os.path.isdir(p):
            hints.append(p)
    return hints


def detect_hardware():
    hw = {
        "os": f"{platform.system()} {platform.release()}",
        "arch": platform.machine(),
        "mem_gb": detect_mem_gb(),
        "gpu": detect_gpu(),
        "disk_free_gb": None,
    }
    try:
        hw["disk_free_gb"] = shutil.disk_usage(os.path.expanduser("~")).free / (1024 ** 3)
    except Exception:
        pass
    hw["network"] = detect_network()
    return hw


def tier_of(hw):
    """设备档位（2026-08-12 更新语义）：a(≥8G·Ollama完整版) / b(≥4G·Q8缩量或Ollama) /
    c(≥2G·纯FTS) / d(<2G·强烈警告)"""
    mem = hw.get("mem_gb") or 0
    if mem >= 8 and (hw.get("gpu") or mem >= 12):
        return "a"
    if mem >= 4:
        return "b"
    if mem >= 2:
        return "c"
    return "d"


# ---------------------------------------------------------------- 交互（questionary）

def _ask_select(message, choices, default, yes):
    if yes:
        return default
    import questionary
    return questionary.select(message, choices=choices, default=default).ask()


def _ask_checkbox(message, choices, yes, default_all=False):
    """多选（Step 4 旧记忆路径选择）"""
    if yes:
        return list(choices) if default_all else []
    import questionary
    return questionary.checkbox(message, choices=choices).ask()


def _ask_confirm(message, default, yes):
    if yes:
        return default
    import questionary
    return questionary.confirm(message, default=default).ask()


def _ask_text(message, default, yes, placeholder=None):
    if yes:
        return default
    import questionary
    return questionary.text(message, default=default, placeholder=placeholder or "").ask()


# ---------------------------------------------------------------- 向导主流程

def run_wizard(yes=False):
    print(SCOPE_TEXT)

    # ---- Step 0 第一环：适用范围/门槛/风险确认 ----
    hw = detect_hardware()
    tier = tier_of(hw)
    warnings = []
    if hw["mem_gb"] and hw["mem_gb"] < 4:
        warnings.append(f"⚠️  你的内存只有 {hw['mem_gb']:.1f}GB，低于推荐 4GB："
                        f"只能运行「纯 FTS 极简模式」或 Q4 低配版，检索质量明显低于向量模式")
    if hw["mem_gb"] and hw["mem_gb"] < 2:
        warnings.append("⚠️⚠️ 低于最低配置（2GB）：强烈不建议安装，体验无法保证，请知险而装")
    if hw["disk_free_gb"] is not None and hw["disk_free_gb"] < 2:
        warnings.append(f"⚠️  磁盘剩余仅 {hw['disk_free_gb']:.1f}GB，低于 2GB 要求")

    if warnings:
        print("\n".join(warnings))
    if not _ask_confirm("我了解适用范围与设备要求，继续安装？", default=True, yes=yes):
        print("已取消安装。")
        return 1

    # ---- Step 1 语言（直接问，不检测不预填）----
    lang_choice = _ask_select("主要使用语言？", LANGUAGE_CHOICES,
                              default=LANGUAGE_CHOICES[0], yes=yes)
    lang = LANGUAGE_KEYS[LANGUAGE_CHOICES.index(lang_choice)]
    model_by_lang = {"zh": "bge-m3", "en": "bge-en/nomic", "mixed": "bge-m3", "multi": "bge-m3"}
    fts_by_lang = {"zh": "trigram", "en": "standard", "mixed": "trigram", "multi": "trigram"}

    # ---- Step 2 探测展示 ----
    print("\n🔍 检测到你的机器：", flush=True)
    print(f"   OS: {hw['os']} ({hw['arch']})")
    print(f"   内存: {hw['mem_gb']:.1f}GB" if hw["mem_gb"] else "   内存: 未知")
    print(f"   GPU: {hw['gpu'] or '无（CPU 模式）'}")
    if hw["disk_free_gb"]:
        print(f"   磁盘剩余: {hw['disk_free_gb']:.0f}GB")
    net = hw["network"]
    print(f"   网络: modelscope {'✅' if net['modelscope'] else '❌'} / "
          f"huggingface {'✅' if net['huggingface'] else '❌'}")

    # ---- Step 3 第二环：设备档位 + 推荐配置确认 ----
    # 2026-08-12 定案：嵌入模型三档（F16默认/Q8降级/fts-only兜底），安装时只给建议与对接链路，
    # 不内置模型文件——模型由用户按需拉取（ollama pull / 挂载 GGUF / 自动降级）
    tier_desc = {
        "a": ("bge-m3 完整版（Ollama 推荐·检索质量最佳）", "ollama"),
        "b": ("bge-m3 缩量版（Q8·省内存）", "llama.cpp"),
        "c": ("纯 FTS 极简模式（不加载嵌入模型）", "fts-only"),
        "d": ("纯 FTS 极简模式（强烈建议不要安装）", "fts-only"),
    }
    rec_name, rec_provider = tier_desc[tier]
    print(f"\n📋 推荐配置：{rec_name}（语言: {lang_choice.split('（')[0]}）")
    if not _ask_confirm(f"使用推荐配置？[推荐: {rec_name}]", default=True, yes=yes):
        provider_choice = _ask_select("选择嵌入档位（按设备负担从小到大）：", [
            "① ollama 外部服务（bge-m3 完整版，推荐·1条命令安装·质量最佳）",
            "② llama.cpp 本地（bge-m3 Q8 缩量版·省内存·需 pip install -e .[embed]）",
            "③ fts-only 纯关键词（最省资源·不加载模型）",
        ], default=("① ollama 外部服务（bge-m3 完整版，推荐·1条命令安装·质量最佳）"
                    if tier in ("a", "b") else
                    "③ fts-only 纯关键词（最省资源·不加载模型）"), yes=yes)
        if "①" in provider_choice:
            rec_provider = "ollama"
        elif "②" in provider_choice:
            rec_provider = "llama.cpp"
        else:
            rec_provider = "fts-only"

    # 档位对接链路提示（不内置模型，用户按需拉取）
    print("\n🔌 嵌入档位对接（模型不随包内置，按需安装）：")
    if rec_provider == "ollama":
        # 审计八审🟡（2026-08-12）：选 Ollama 但未装 → 当场探测，避免静默降级
        import shutil as _sh
        ollama_ok = False
        if _sh.which("ollama"):
            try:
                import subprocess as _sp
                r = _sp.run(["ollama", "list"], capture_output=True, text=True, timeout=10)
                if "bge-m3" in r.stdout:
                    ollama_ok = True
                    print("   ✅ 检测到 Ollama 且 bge-m3 已就绪")
                elif r.returncode == 0:
                    print("   ⚠️ 检测到 Ollama 但未拉取 bge-m3 → 稍后运行: ollama pull bge-m3")
                else:
                    print(f"   ⚠️ Ollama 未运行（{r.stderr.strip()[:60]}）→ 启动后: ollama pull bge-m3")
            except Exception as e:
                print(f"   ⚠️ Ollama 探测失败: {e}")
        else:
            print("   ⚠️ 未检测到 Ollama！当前选择①将无法生效（会静默降级 fts-only）")
            print("   安装指引: brew install ollama && ollama pull bge-m3（macOS/Linux）")
            print("            或访问 https://ollama.com/download 一键安装后重新运行 init")
            if _ask_confirm("是否改为 fts-only 极简档（无需 Ollama）？", default=False, yes=yes):
                rec_provider = "fts-only"
                print("   → 已切换 fts-only（后续装好 Ollama 可随时改 config.yaml 升级）")
    elif rec_provider == "llama.cpp":
        print("   · pip install -e .[embed] 自动拉取 bge-m3 Q8 缩量版（约 605MB）")
        print("   · 或手动挂载: ollama create bge-m3-q8 -f <(echo \"FROM <GGUF路径>\")")
    else:
        print("   · 无需模型（纯关键词检索），后续想升级语义检索可随时改 config.yaml")

    # ---- Step 4 旧记忆导入（多选路径 + 自动识别） ----
    old_hints = detect_old_memory()
    import_paths = []
    if old_hints and _ask_confirm(
            f"检测到旧记忆痕迹（{len(old_hints)} 处），是否导入旧记忆？",
            default=True, yes=yes):
        choices = old_hints
        picked = _ask_checkbox("选择要导入的旧记忆路径（空格多选，回车确认）：",
                               choices, yes, default_all=(len(old_hints) <= 3))
        import_paths = [c for c in picked if c in old_hints] or []
        # 允许自定义额外路径
        extra = _ask_text("如需额外导入其他目录/记忆包（memories.zip），输入路径（留空跳过）：",
                          default="", yes=yes,
                          placeholder="如 ~/memories.zip 或 ~/.openclaw/workspace/memory")
        if extra.strip():
            import_paths.append(os.path.expanduser(extra.strip()))
    elif not old_hints and _ask_confirm("未检测到旧记忆。是否手动指定旧记忆路径导入？",
                                        default=False, yes=yes):
        extra = _ask_text("旧记忆目录或记忆包（memories.zip）路径：",
                          default="", yes=yes,
                          placeholder="如 ~/memories.zip 或 ~/.openclaw/workspace/memory")
        if extra.strip():
            import_paths.append(os.path.expanduser(extra.strip()))

    # ---- Step 5 落地：写配置 → 模型 → 初始化 → 自检 ----
    print("\n⚙️  开始落地...")
    config = {
        "language": lang,
        "embedding": {
            "provider": rec_provider,
            "model": model_by_lang[lang],
            "dim": 1024,
        },
        "fts": {"tokenizer": fts_by_lang[lang]},
        "server": {"host": "127.0.0.1", "port": 8765},
        "hardware_detected": {
            "os": hw["os"], "arch": hw["arch"], "mem_gb": round(hw["mem_gb"] or 0, 1),
            "gpu": hw["gpu"], "tier": tier,
        },
    }
    return apply_install(config, import_paths, yes=yes)


def apply_install(config, import_paths=None, yes=False):
    """落地执行（与交互解耦，可单测）：写配置 → 模型确认 → 初始化库 → 导入旧记忆 → 增强管线 → 自检

    import_paths: 旧记忆路径列表（目录或 memories.zip），导入后自动跑增强管线
    返回 exit code：0 = 安装完成；非 0 = 自检有告警
    """
    os.makedirs(CONFIG_DIR, exist_ok=True)
    # 转 YAML（手写，避免引入 pyyaml 依赖）
    yaml_lines = []
    def dump(d, indent=0):
        for k, v in d.items():
            pad = "  " * indent
            if isinstance(v, dict):
                yaml_lines.append(f"{pad}{k}:")
                dump(v, indent + 1)
            else:
                yaml_lines.append(f"{pad}{k}: {v}")
    dump(config)
    with open(CONFIG_PATH, "w") as f:
        f.write("\n".join(yaml_lines) + "\n")
    print(f"   ✅ 配置写入: {CONFIG_PATH}")

    rec_provider = config["embedding"]["provider"]
    # 模型确认（llama.cpp 模式）
    if rec_provider == "llama.cpp":
        model_path = os.path.join(MODEL_DIR, DEFAULT_GGUF)
        if os.path.exists(model_path):
            size_mb = os.path.getsize(model_path) / 1024 / 1024
            print(f"   ✅ 嵌入模型已就绪: {DEFAULT_GGUF} ({size_mb:.0f}MB)")
        else:
            print(f"\n   ⏳ 嵌入模型缺失: {DEFAULT_GGUF}，开始自动下载（断点续传）...")
            from memory_server.downloader import download_model
            r = download_model([GGUF_DOWNLOAD_HINT["modelscope"], GGUF_DOWNLOAD_HINT["huggingface"]],
                               model_path, progress=True)
            if r["status"] in ("ok", "skipped"):
                print(f"   ✅ 模型下载完成: {r['msg']}")
            else:
                print(f"   ⚠️ 模型下载未完成: {r['msg']}")
                print(f"   可稍后重试: memory-server download-model（断点续传）")

    # 初始化数据库
    db.init_db()
    print(f"   ✅ 数据库初始化: {db.DB_PATH}")

    # 旧记忆导入（多路径，自动识别格式，导入后自动跑增强管线）
    if import_paths:
        from memory_server.embed import detect_provider
        provider = detect_provider(prefer="fts-only" if rec_provider == "fts-only" else None)
        from memory_server.importflow import run_import, post_import_enhance
        print(f"\n📥 导入旧记忆（{len(import_paths)} 处，格式自动识别）...")
        report = run_import(import_paths, provider)
        print(f"   ✅ 导入汇总: 新摄入 {report['ok']} / 跳过 {report['skipped']} / 失败 {len(report['failed'])}")
        for ns, n in report["by_namespace"].items():
            print(f"      namespace[{ns}]: +{n}")
        if report["failed"]:
            for path, err in report["failed"][:5]:
                print(f"   ⚠️  {path}: {err}")
        print("   ⚙️  自动增强管线（分类/规则实体KG/AAAK压缩/语义去重/向量索引）...")
        post_import_enhance(provider, apply_dedup=False)
        print("   ✅ 增强管线完成")

    # 自检
    print("\n🩺 运行自检...")
    try:
        from memory_server.doctor import main as doctor_main
        rc = doctor_main(argv=[])
        if rc == 0:
            print("\n🎉 安装完成！启动服务: memory-server serve")
            return 0
        print("\n⚠️  自检有告警，请查看上方明细")
        return rc
    except Exception as e:
        print(f"   ⚠️ 自检调用失败: {e}")
        print("   安装已完成，可手动运行 memory-server doctor")
        return 0


def main():
    import argparse
    parser = argparse.ArgumentParser(prog="eidetic init", description="安装向导（零问+确认）")
    parser.add_argument("--yes", action="store_true", help="非交互全默认（懒人/CI）")
    args = parser.parse_args()
    return run_wizard(yes=args.yes)


if __name__ == "__main__":
    sys.exit(main())
