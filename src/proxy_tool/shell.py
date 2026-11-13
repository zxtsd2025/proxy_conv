"""生成可被当前 shell 执行的 shell 代码。

子进程无法修改父 shell 的环境变量，所以真正的做法是：
Python 只把 ``export``/``set -gx`` 语句打印到 stdout，再由 rc 文件里的
``proxy()`` 函数在当前 shell 中执行。本模块同时支持 POSIX shell
（bash/zsh）和 fish。
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import Iterable, TYPE_CHECKING

from .config import BASE_ENV_VARS

if TYPE_CHECKING:  # pragma: no cover
    from .config import Profile

ACTIVE_VAR = "PROXY_TOOL_ACTIVE"
PROFILE_VAR = "PROXY_TOOL_PROFILE"
VARS_VAR = "PROXY_TOOL_VARS"
RESERVED_VARS = {ACTIVE_VAR, PROFILE_VAR, VARS_VAR}

SAVED_PREFIX = "_PROXY_TOOL_SAVED_"
SAVED_SET_PREFIX = "_PROXY_TOOL_SAVED_SET_"

SHELL_TYPES = ("posix", "fish")

ENV_COMMANDS = (
    "on",
    "start",
    "enable",
    "off",
    "stop",
    "disable",
    "use",
    "switch",
    "reload",
    "refresh",
    "toggle",
)

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

BLOCK_START = "# >>> proxy-tool >>>"
BLOCK_END = "# <<< proxy-tool <<<"
_BLOCK_RE = re.compile(
    r"(?ms)^# >>> proxy-tool >>>[^\n]*\n.*?^# <<< proxy-tool <<<[^\n]*\n?"
)


def _valid_name(name: str) -> bool:
    if not _ENV_NAME_RE.match(name):
        return False
    if name in RESERVED_VARS:
        return False
    if name.startswith(("PROXY_TOOL_", "_PROXY_TOOL_", "_proxy_tool_")):
        return False
    return True


def _dedupe(names: Iterable[str]) -> list[str]:
    result: list[str] = []
    for name in names:
        if _valid_name(name) and name not in result:
            result.append(name)
    return result


def _check_shell(shell: str) -> None:
    if shell not in SHELL_TYPES:
        raise ValueError(f"不支持的 shell 类型: {shell}")


def active_vars_from_env(environ: dict[str, str] | None = None) -> list[str]:
    """读取当前 shell 通过 PROXY_TOOL_VARS 留下的受管变量列表。"""
    env = os.environ if environ is None else environ
    raw = env.get(VARS_VAR, "")
    return _dedupe(raw.split())


# ---------------------------------------------------------------------------
# POSIX shell（bash / zsh）
# ---------------------------------------------------------------------------


def _posix_export(name: str, value: str) -> str:
    return f"export {name}={shlex.quote(value)}"


def _posix_unset(name: str) -> str:
    return f"unset {name}"


def _posix_save_previous(name: str) -> str:
    set_marker = SAVED_SET_PREFIX + name
    saved = SAVED_PREFIX + name
    return (
        f'if [ -z "${{{set_marker}+x}}" ]; then\n'
        f"    {set_marker}=${{{name}+x}}\n"
        f"    {saved}=${{{name}-}}\n"
        f"fi"
    )


def _posix_restore_previous(name: str) -> str:
    set_marker = SAVED_SET_PREFIX + name
    saved = SAVED_PREFIX + name
    return (
        f'if [ "${{{set_marker}-}}" = x ]; then\n'
        f'    {name}="${{{saved}-}}"\n'
        f"    export {name}\n"
        f"else\n"
        f"    unset {name}\n"
        f"fi\n"
        f"unset {set_marker} {saved}"
    )


# ---------------------------------------------------------------------------
# fish
# ---------------------------------------------------------------------------


def _fish_quote(value: str) -> str:
    """fish 单引号字符串：反斜杠和单引号需要转义。"""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _fish_export(name: str, value: str) -> str:
    return f"set -gx {name} {_fish_quote(value)}"


def _fish_unset(name: str) -> str:
    # fish 的 set -e 对不存在的变量返回 4，所以调用方会在末尾补一个 true
    return f"set -e {name}"


def _fish_save_previous(name: str) -> str:
    set_marker = SAVED_SET_PREFIX + name
    saved = SAVED_PREFIX + name
    return (
        f"if not set -q {set_marker}\n"
        f"    if set -q {name}\n"
        f"        set -g {set_marker} 1\n"
        f'        set -g {saved} "$' + name + '"\n'
        f"    else\n"
        f"        set -g {set_marker} 0\n"
        f"    end\n"
        f"end"
    )


def _fish_restore_previous(name: str) -> str:
    set_marker = SAVED_SET_PREFIX + name
    saved = SAVED_PREFIX + name
    return (
        f'if test "$' + set_marker + '" = 1\n'
        f"    set -gx {name} $" + saved + "\n"
        f"else\n"
        f"    set -e {name}\n"
        f"end\n"
        f"set -e {set_marker}\n"
        f"set -e {saved}"
    )


# ---------------------------------------------------------------------------
# 公共渲染
# ---------------------------------------------------------------------------


def render_activate(
    profile: "Profile",
    *,
    current_vars: Iterable[str] = (),
    save_previous: bool = True,
    shell: str = "posix",
) -> str:
    """生成启用某个配置的 shell 代码。"""
    _check_shell(shell)
    values = profile.proxy_env()
    managed = _dedupe(
        list(BASE_ENV_VARS) + list(current_vars) + list(values.keys())
    )

    chunks: list[str] = []
    if shell == "fish":
        if save_previous:
            chunks.extend(_fish_save_previous(name) for name in managed)
        for name in managed:
            chunks.append(
                _fish_export(name, values[name]) if name in values else _fish_unset(name)
            )
        chunks.append(_fish_export(ACTIVE_VAR, "1"))
        chunks.append(_fish_export(PROFILE_VAR, profile.name))
        chunks.append(_fish_export(VARS_VAR, " ".join(managed)))
        chunks.append("true")
        return "\n".join(chunks) + "\n"

    if save_previous:
        chunks.extend(_posix_save_previous(name) for name in managed)
    for name in managed:
        chunks.append(
            _posix_export(name, values[name]) if name in values else _posix_unset(name)
        )
    chunks.append(_posix_export(ACTIVE_VAR, "1"))
    chunks.append(_posix_export(PROFILE_VAR, profile.name))
    chunks.append(_posix_export(VARS_VAR, " ".join(managed)))
    return "\n".join(chunks) + "\n"


def render_deactivate(
    current_vars: Iterable[str] = (),
    *,
    shell: str = "posix",
) -> str:
    """生成停用代理、恢复启用前环境变量的 shell 代码。"""
    _check_shell(shell)
    managed = _dedupe(list(BASE_ENV_VARS) + list(current_vars))
    if shell == "fish":
        chunks = [_fish_restore_previous(name) for name in managed]
        chunks.append(f"set -e {ACTIVE_VAR} {PROFILE_VAR} {VARS_VAR}")
        chunks.append("true")
        return "\n".join(chunks) + "\n"

    chunks = [_posix_restore_previous(name) for name in managed]
    chunks.append(f"unset {ACTIVE_VAR} {PROFILE_VAR} {VARS_VAR}")
    return "\n".join(chunks) + "\n"


def render_plain_env(profile: "Profile", *, shell: str = "posix") -> str:
    """只输出导出语句，供 ``eval`` / ``source`` 使用。"""
    _check_shell(shell)
    values = profile.proxy_env()
    managed = _dedupe(list(BASE_ENV_VARS) + list(values.keys()))
    if shell == "fish":
        chunks = [
            _fish_export(name, values[name]) if name in values else _fish_unset(name)
            for name in managed
        ]
        chunks.append(_fish_export(ACTIVE_VAR, "1"))
        chunks.append(_fish_export(PROFILE_VAR, profile.name))
        chunks.append(_fish_export(VARS_VAR, " ".join(managed)))
        chunks.append("true")
        return "\n".join(chunks) + "\n"

    chunks = [
        _posix_export(name, values[name]) if name in values else _posix_unset(name)
        for name in managed
    ]
    chunks.append(_posix_export(ACTIVE_VAR, "1"))
    chunks.append(_posix_export(PROFILE_VAR, profile.name))
    chunks.append(_posix_export(VARS_VAR, " ".join(managed)))
    return "\n".join(chunks) + "\n"


def _posix_shell_block(
    cli_path: str | os.PathLike[str], bin_dir: str | os.PathLike[str]
) -> str:
    cli = shlex.quote(str(cli_path))
    bindir = shlex.quote(str(bin_dir))
    env_commands = "|".join(ENV_COMMANDS)
    return f"""\
{BLOCK_START}
# Managed by proxy-tool. Do not edit this block manually.
# Re-run `proxy-cli init --install` to update it.
_proxy_tool_bin={bindir}
case ":${{PATH-}}:" in
    *":${{_proxy_tool_bin}}:"*) ;;
    *) export PATH="${{_proxy_tool_bin}}:${{PATH-}}" ;;
esac
unset _proxy_tool_bin

proxy() {{
    if [ "$#" -eq 0 ]; then
        set -- on
    fi
    local _proxy_tool_out _proxy_tool_rc _proxy_tool_arg PROXY_TOOL_SHELL
    if [ -n "${{ZSH_VERSION-}}" ]; then
        PROXY_TOOL_SHELL=zsh
    else
        PROXY_TOOL_SHELL=bash
    fi
    export PROXY_TOOL_SHELL
    case "$1" in
        {env_commands})
            for _proxy_tool_arg in "$@"; do
                case "$_proxy_tool_arg" in
                    -h|--help)
                        command {cli} "$@"
                        return
                        ;;
                esac
            done
            _proxy_tool_rc=0
            _proxy_tool_out="$(command {cli} --emit-shell "$@")" || _proxy_tool_rc=$?
            if [ -n "$_proxy_tool_out" ]; then
                eval "$_proxy_tool_out"
            fi
            return "$_proxy_tool_rc"
            ;;
        *)
            command {cli} "$@"
            ;;
    esac
}}
{BLOCK_END}
"""


def _fish_shell_block(
    cli_path: str | os.PathLike[str], bin_dir: str | os.PathLike[str]
) -> str:
    cli = _fish_quote(str(cli_path))
    bindir = _fish_quote(str(bin_dir))
    env_commands = " ".join(ENV_COMMANDS)
    return f"""\
{BLOCK_START}
# Managed by proxy-tool. Do not edit this block manually.
# Re-run `proxy-cli init --install` to update it.
if not contains -- {bindir} $PATH
    set -gx PATH {bindir} $PATH
end

function proxy
    set -lx PROXY_TOOL_SHELL fish
    set -l _proxy_tool_args $argv
    if test (count $_proxy_tool_args) -eq 0
        set _proxy_tool_args on
    end
    switch $_proxy_tool_args[1]
        case {env_commands}
            set -l _proxy_tool_arg
            for _proxy_tool_arg in $_proxy_tool_args
                switch $_proxy_tool_arg
                    case -h --help
                        command {cli} $_proxy_tool_args
                        return
                end
            end
            command {cli} --emit-shell --shell-type fish $_proxy_tool_args | source
            set -l _proxy_tool_ps $pipestatus
            if test "$_proxy_tool_ps[1]" -ne 0
                return $_proxy_tool_ps[1]
            end
            return $_proxy_tool_ps[2]
        case env
            command {cli} --shell-type fish $_proxy_tool_args
        case '*'
            command {cli} $_proxy_tool_args
    end
end
{BLOCK_END}
"""


def shell_block(
    cli_path: str | os.PathLike[str],
    bin_dir: str | os.PathLike[str],
    *,
    shell: str = "posix",
) -> str:
    """生成要写入 shell rc 文件的 proxy 函数。"""
    _check_shell(shell)
    if shell == "fish":
        return _fish_shell_block(cli_path, bin_dir)
    return _posix_shell_block(cli_path, bin_dir)


def remove_shell_block(text: str) -> str:
    """删除旧的集成块；没有则原样返回。"""
    return _BLOCK_RE.sub("", text)


def install_shell_block(text: str, block: str) -> str:
    """替换旧的集成块；保留 rc 文件里其他内容。"""
    cleaned = remove_shell_block(text)
    if cleaned.strip():
        return cleaned.rstrip() + "\n\n" + block.strip() + "\n"
    return block.strip() + "\n"


def detect_shell() -> str | None:
    for name in (os.environ.get("PROXY_TOOL_SHELL"), Path(os.environ.get("SHELL", "")).name):
        if name in {"bash", "zsh", "fish"}:
            return name
    return None


def rc_path_for_shell(shell: str) -> Path:
    if shell == "bash":
        return Path.home() / ".bashrc"
    if shell == "zsh":
        zdotdir = os.environ.get("ZDOTDIR")
        base = Path(zdotdir).expanduser() if zdotdir else Path.home()
        return base / ".zshrc"
    if shell == "fish":
        xdg = os.environ.get("XDG_CONFIG_HOME")
        base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
        return base / "fish" / "config.fish"
    raise ValueError(f"不支持的 shell: {shell}")


def existing_rc_paths() -> list[Path]:
    """返回可能安装过集成块的 bash/zsh/fish rc 文件（仅存在的）。"""
    candidates = [Path.home() / ".bashrc"]
    zdotdir = os.environ.get("ZDOTDIR")
    candidates.append((Path(zdotdir).expanduser() if zdotdir else Path.home()) / ".zshrc")
    xdg = os.environ.get("XDG_CONFIG_HOME")
    fish_base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    candidates.append(fish_base / "fish" / "config.fish")
    result: list[Path] = []
    for path in candidates:
        if path.exists() and path not in result:
            result.append(path)
    return result
