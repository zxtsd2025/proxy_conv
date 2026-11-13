from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

CLEAN_ENV_KEYS = (
    "PROXY_CONFIG",
    "PROXY_TOOL_ACTIVE",
    "PROXY_TOOL_PROFILE",
    "PROXY_TOOL_VARS",
    "PROXY_TOOL_SHELL",
    "http_proxy",
    "HTTP_PROXY",
    "https_proxy",
    "HTTPS_PROXY",
    "ftp_proxy",
    "FTP_PROXY",
    "all_proxy",
    "ALL_PROXY",
    "no_proxy",
    "NO_PROXY",
)


def base_env(home: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in CLEAN_ENV_KEYS:
        env.pop(key, None)
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["PYTHONPATH"] = str(SRC)
    env["NO_COLOR"] = "1"
    env["SHELL"] = "/bin/bash"
    return env


def run_cli(
    args: Iterable[str],
    home: Path,
    *,
    extra_env: dict[str, str] | None = None,
    timeout: float = 30,
) -> subprocess.CompletedProcess[str]:
    env = base_env(home)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "proxy_tool", *args],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def run_bash(
    script: str,
    home: Path,
    *,
    extra_env: dict[str, str] | None = None,
    timeout: float = 30,
) -> subprocess.CompletedProcess[str]:
    env = base_env(home)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", "-c", script],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def json_stdout(proc: subprocess.CompletedProcess[str]) -> object:
    return json.loads(proc.stdout)


def make_launcher(home: Path) -> Path:
    """创建一个测试用 proxy-cli 启动器，指向仓库源码。"""
    bin_dir = home / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    launcher = bin_dir / "proxy-cli"
    launcher.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"sys.path.insert(0, {str(SRC)!r})\n"
        "from proxy_tool.cli import main\n"
        "sys.exit(main())\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    return launcher
