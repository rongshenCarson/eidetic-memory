#!/bin/bash
# ============================================================
# Eidetic-Memory 一键安装脚本
# 用法: bash <(curl -sL https://github.com/rongshenCarson/eidetic-memory/install.sh)
#   或: ./install.sh
# 自动完成: Python 检查 → venv → 依赖 → init 向导 → openclaw-setup → 服务安装
# ============================================================
set -e

# 颜色
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info() { echo -e "${GREEN}[eidetic]${NC} $1"; }
warn() { echo -e "${YELLOW}[eidetic]${NC} $1"; }
err()  { echo -e "${RED}[eidetic]${NC} $1"; exit 1; }

# ---- 1. Python 检查（找 3.11+，不绑定系统默认 python3）----
PY=""
for cand in python3.13 python3.12 python3.11 python3; do
    if command -v $cand &>/dev/null; then
        VER=$($cand -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null)
        MAJOR=${VER%%.*}; MINOR=${VER##*.}
        if [ "$MAJOR" -gt 3 ] || { [ "$MAJOR" -eq 3 ] && [ "$MINOR" -ge 11 ]; }; then
            PY=$cand; break
        fi
    fi
done
if [ -z "$PY" ]; then
    err "未找到 Python 3.11+。请安装: https://www.python.org/downloads/"
fi
info "✅ Python $($PY --version 2>&1 | cut -d' ' -f2) ($PY)"

# ---- 2. 获取源码 ----
# 支持三种来源: 已clone目录 / git clone / 下载release包
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/pyproject.toml" ] && grep -q "eidetic-memory" "$SCRIPT_DIR/pyproject.toml" 2>/dev/null; then
    APP_DIR="$SCRIPT_DIR"
    info "✅ 使用当前目录源码: $APP_DIR"
elif command -v git &>/dev/null; then
    APP_DIR="$HOME/eidetic-memory"
    if [ ! -d "$APP_DIR" ]; then
        info "📥 克隆仓库..."
        git clone --depth 1 https://github.com/rongshenCarson/eidetic-memory.git "$APP_DIR"
    else
        info "✅ 仓库已存在: $APP_DIR"
    fi
else
    err "未找到源码且无 git。请先下载 release 包解压后运行此脚本"
fi
cd "$APP_DIR"

# ---- 3. venv + 依赖 ----
if [ ! -d ".venv" ]; then
    info "🔧 创建虚拟环境（$PY）..."
    $PY -m venv .venv
fi
source .venv/bin/activate
info "📦 安装依赖（fastmcp/questionary/numpy，轻量）..."
pip install -q --upgrade pip
pip install -q -r requirements.txt
# 2026-08-12 修复：注册项目包（--no-deps）——否则 OpenClaw 从非安装目录启动 MCP 时
# 找不到 memory_server 模块 → Connection closed
pip install -q -e . --no-deps

# ---- 4. Ollama 检测（引导，不强制）----
if command -v ollama &>/dev/null; then
    if ollama list 2>/dev/null | grep -q "bge-m3"; then
        info "✅ Ollama + bge-m3 已就绪（完整语义检索）"
    else
        warn "检测到 Ollama 但未拉取 bge-m3 → 运行: ollama pull bge-m3"
    fi
else
    warn "未检测到 Ollama。安装向导会引导选择（或先跑: brew install ollama）"
fi

# ---- 5. 部署（用户级+交互，由 agent 或用户执行）----
# 2026-08-12 重构：init/openclaw-setup/install-service 涉及用户 HOME 配置与交互，
# 本脚本（可能以任意权限跑）不代劳——提示下一步，交给 agent 最稳妥。
echo ""
info "🎉 环境就绪！下一步部署（二选一）："
echo ""
info "  👉 方式一（推荐）：让 agent 完成部署"
info "     对 agent 说：「用 eidetic-memory 部署记忆系统」"
info "     agent 按 docs/AGENT-DEPLOY-GUIDE.md 自动完成："
info "       初始化 → OpenClaw 接入 → 服务安装 → 验证"
echo ""
info "  👉 方式二：手动部署（3 条命令）"
info "     $APP_DIR/.venv/bin/python -m memory_server init"
info "     $APP_DIR/.venv/bin/python -m memory_server openclaw-setup"
info "     $APP_DIR/.venv/bin/python -m memory_server install-service"
echo ""
info "  部署完成后验证: curl http://127.0.0.1:8765/health"
echo ""
