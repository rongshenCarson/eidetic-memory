#!/usr/bin/env python3
"""
memory-server install-service（P1 收尾 2026-08-09 完善）
==========================================================
生成 + 安装/卸载/查询 OS 原生守护（不自研守护进程）：
  macOS:   launchd KeepAlive
  Linux:   systemd user unit
  Windows: NSSM（提示手动执行，NSSM 需预装）

用法: memory-server install-service [--action install|uninstall|status]
"""
import os
import sys
import platform
import subprocess

SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_PY = os.path.join(SERVER_DIR, ".venv", "bin", "python")
if not os.path.exists(VENV_PY):
    VENV_PY = os.path.join(SERVER_DIR, ".venv", "Scripts", "python.exe")

LABEL = "ai.eidetic.memory"  # 2026-08-12 九审🔵：与 pkg identifier 统一（原 ai.eidetic.server）
LOG_DIR = os.path.join(SERVER_DIR, "data")


def _venv_python():
    return VENV_PY


def macos_launchd():
    # 自定义数据目录（若设置则传播到服务，避免 CLI 连新库、服务连旧库 split-brain）
    _extra_env = "\n".join(
        f"        <key>{k}</key>\n        <string>{os.environ[k]}</string>"
        for k in ("MEMORY_SERVER_DB_DIR", "MEMORY_SERVER_RAW_DIR")
        if os.environ.get(k))
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{_venv_python()}</string>
        <string>-m</string>
        <string>memory_server</string>
        <string>serve</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{SERVER_DIR}</string>
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{LOG_DIR}/server.log</string>
    <key>StandardErrorPath</key>
    <string>{LOG_DIR}/server.err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>{os.path.expanduser('~')}</string>
        <key>PATH</key>
        <string>{os.environ.get('PATH', '/usr/bin:/bin')}</string>
        <key>MEMORY_AGENT_INGEST</key>
        <string>1</string>
        <key>MEMORY_EXTRACT</key>
        <string>{os.environ.get('MEMORY_EXTRACT', '')}</string>
{_extra_env}
    </dict>
</dict>
</plist>
"""
    path = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(plist)
    os.chmod(path, 0o644)
    return path


def linux_systemd():
    # 自定义数据目录（若设置则传播到服务，与 launchd 一致）
    _extra_env = "\n".join(
        f"Environment={k}={os.environ[k]}"
        for k in ("MEMORY_SERVER_DB_DIR", "MEMORY_SERVER_RAW_DIR")
        if os.environ.get(k))
    unit = f"""[Unit]
Description=memory-server (memory server)
After=network.target

[Service]
Type=simple
WorkingDirectory={SERVER_DIR}
ExecStart={_venv_python()} -m memory_server serve
Restart=always
RestartSec=10
Environment=HOME={os.path.expanduser('~')}
{_extra_env}

[Install]
WantedBy=default.target
"""
    path = os.path.expanduser("~/.config/systemd/user/memory-server.service")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(unit)
    return path


def windows_nssm():
    print("Windows 用 NSSM 注册服务（需已安装 NSSM）：")
    print(f"  nssm install memory-server {_venv_python()} -m memory_server serve")
    print(f"  nssm set memory-server AppDirectory {SERVER_DIR}")
    print("  nssm start memory-server")
    return None


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except Exception as e:
        return None


def install():
    sysname = platform.system()
    print(f"🖥️ 平台: {sysname}")
    if sysname == "Darwin":
        # 审计十审🟡（2026-08-12）：清理旧 LABEL（ai.eidetic.server）——改名后旧服务
        # 仍可能运行，与新服务并存撞 8765 端口（KeepAlive 互相拉起崩溃循环）
        OLD_LABELS = ["ai.eidetic.server"]
        for old in OLD_LABELS:
            old_plist = os.path.expanduser(f"~/Library/LaunchAgents/{old}.plist")
            _run(["launchctl", "bootout", f"gui/{os.getuid()}/{old}"]) if sys.platform == "darwin" else None
            _run(["launchctl", "unload", old_plist])
            if os.path.exists(old_plist):
                try:
                    os.remove(old_plist)
                    print(f"🧹 已清理旧服务 {old}")
                except Exception:
                    pass
        path = macos_launchd()
        print(f"✅ launchd 配置: {path}")
        # 先卸载旧的（幂等），再加载
        _run(["launchctl", "unload", path])
        r = _run(["launchctl", "load", path])
        if r and r.returncode == 0:
            print("✅ 服务已加载并启动（KeepAlive 自动拉起）")
        else:
            print(f"⚠️ launchctl load 失败: {r.stderr if r else '未知'}")
    elif sysname == "Linux":
        path = linux_systemd()
        print(f"✅ systemd 配置: {path}")
        _run(["systemctl", "--user", "daemon-reload"])
        r = _run(["systemctl", "--user", "enable", "--now", "memory-server"])
        print("✅ 服务已启用" if r and r.returncode == 0 else f"⚠️ 启用失败: {r.stderr if r else ''}")
    elif sysname == "Windows":
        windows_nssm()
    else:
        print(f"❌ 不支持的平台: {sysname}")
        return 1
    return 0


def uninstall():
    sysname = platform.system()
    if sysname == "Darwin":
        path = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
        _run(["launchctl", "unload", path])
        if os.path.exists(path):
            os.remove(path)
        print("✅ launchd 服务已卸载")
    elif sysname == "Linux":
        _run(["systemctl", "--user", "stop", "memory-server"])
        _run(["systemctl", "--user", "disable", "memory-server"])
        path = os.path.expanduser("~/.config/systemd/user/memory-server.service")
        if os.path.exists(path):
            os.remove(path)
        _run(["systemctl", "--user", "daemon-reload"])
        print("✅ systemd 服务已卸载")
    elif sysname == "Windows":
        print("执行: nssm remove memory-server confirm")
    return 0


def status():
    sysname = platform.system()
    if sysname == "Darwin":
        r = _run(["launchctl", "list", LABEL])
        if r and r.returncode == 0 and r.stdout.strip():
            print(f"✅ 服务运行中: {LABEL}")
            for line in r.stdout.strip().splitlines()[:5]:
                print(f"   {line}")
            return 0
        print(f"❌ 服务未运行: {LABEL}")
        return 1
    elif sysname == "Linux":
        r = _run(["systemctl", "--user", "status", "memory-server", "--no-pager"])
        if r and r.returncode == 0:
            print("✅ 服务运行中 (systemd)")
            return 0
        print("❌ 服务未运行 (systemd)")
        return 1
    print("状态查询暂不支持该平台")
    return 1


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(prog="eidetic install-service",
                                     description="安装/卸载/查询系统守护服务")
    parser.add_argument("--action", default="install", choices=["install", "uninstall", "status"])
    args = parser.parse_args(argv)
    if args.action == "install":
        return install()
    if args.action == "uninstall":
        return uninstall()
    return status()


if __name__ == "__main__":
    sys.exit(main())
