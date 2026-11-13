"""proxy-tool 命令行入口。

面向用户的命令是 ``proxy``（由 shell 函数提供）。这个脚本本身叫
``proxy-cli``，负责读写配置、生成 shell 代码以及提供不依赖 shell
集成的子命令（status/list/add/exec/test/...）。
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence
from urllib.parse import urlsplit

from . import __version__
from .config import (
    BASE_ENV_VARS,
    DEFAULT_NO_PROXY,
    PROFILE_NAME_RE,
    PROXY_SCHEMES,
    Config,
    ConfigError,
    Profile,
    atomic_write_text,
    config_path,
    ensure_config_file,
    load_config,
    mask_env_value,
    mask_url,
    normalize_env,
    normalize_no_proxy,
    normalize_proxy_url,
    save_config,
    set_current,
)
from .net import (
    NetError,
    format_latency,
    http_test,
    profile_endpoints,
    tcp_test,
)
from .shell import (
    ACTIVE_VAR,
    BLOCK_START,
    ENV_COMMANDS,
    PROFILE_VAR,
    VARS_VAR,
    active_vars_from_env,
    detect_shell,
    existing_rc_paths,
    install_shell_block,
    rc_path_for_shell,
    remove_shell_block,
    render_activate,
    render_deactivate,
    render_plain_env,
    shell_block,
)
from .ui import UI

DEFAULT_TEST_URL = "https://www.gstatic.com/generate_204"


class CliError(Exception):
    def __init__(self, message: str, exit_code: int = 1):
        super().__init__(message)
        self.exit_code = exit_code


@dataclass
class App:
    config_override: str | None
    emit_shell: bool
    shell_type: str
    ui: UI

    def config(self, *, create: bool = False) -> Config:
        return load_config(self.config_override, create=create)

    def say(self, text: str = "", *, err: bool = False) -> None:
        """emit-shell 模式下，所有人读信息都必须走 stderr。"""
        stream = sys.stderr if (self.emit_shell or err) else sys.stdout
        print(text, file=stream)

    def error(self, message: str) -> None:
        lines = str(message).splitlines() or [""]
        self.ui.err(self.ui.bad(lines[0], err=True))
        for line in lines[1:]:
            self.ui.err("  " + line)


def _write_shell(code: str) -> None:
    sys.stdout.write(code)
    sys.stdout.flush()


def _active(environ: dict[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return env.get(ACTIVE_VAR) == "1"


def _mask(value: str, reveal: bool) -> str:
    return value if reveal else mask_url(value)


def _display_env(name: str, value: str, reveal: bool) -> str:
    return value if reveal else mask_env_value(name, value)


def _fmt_value(value: str | None, reveal: bool = False) -> str:
    if value is None:
        return "（未设置）"
    return _mask(value, reveal)


def _fmt_json_url(value: str | None, reveal: bool) -> str | None:
    if value is None:
        return None
    return _mask(value, reveal)


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="proxy-cli",
        description="在 Linux 终端中一键启用/停用代理（配置保存在用户目录）。",
        epilog=(
            "常用示例:\n"
            "  proxy                       启用当前配置\n"
            "  proxy stop                  停用并恢复原环境变量\n"
            "  proxy use work              切换并启用 work 配置\n"
            "  proxy test --http           测试代理端口并实际访问网页\n"
            "  proxy exec -- curl https://example.com\n"
            "  proxy add office http://127.0.0.1:7890 --use\n\n"
            "安装 shell 集成: proxy-cli init --install"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="配置文件路径（也可使用环境变量 PROXY_CONFIG）",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="禁用彩色输出",
    )
    parser.add_argument(
        "--emit-shell",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--shell-type",
        choices=("posix", "fish"),
        default="posix",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"proxy-tool {__version__}",
    )

    sub = parser.add_subparsers(dest="command", metavar="<命令>")

    p_on = sub.add_parser(
        "on",
        aliases=["start", "enable"],
        help="启用代理（默认当前配置）",
        description="启用代理。只写环境变量，不修改配置文件。",
    )
    p_on.add_argument("profile", nargs="?", metavar="配置名", help="临时启用指定配置")

    sub.add_parser(
        "stop",
        aliases=["off", "disable"],
        help="停用代理并恢复原环境变量",
        description="停用代理，并尽量恢复启用前的环境变量。",
    )
    sub.add_parser(
        "toggle",
        help="在启用/停用之间切换",
        description="如果当前已启用则停用，否则启用当前配置。",
    )

    p_use = sub.add_parser(
        "use",
        aliases=["switch"],
        help="切换当前配置并立即启用",
        description="把指定配置写为 current，并立即在当前 shell 中启用。",
    )
    p_use.add_argument("profile", metavar="配置名")

    sub.add_parser(
        "reload",
        aliases=["refresh"],
        help="重新加载当前配置",
        description="重新读取配置文件并刷新当前 shell 的环境变量。",
    )

    p_status = sub.add_parser(
        "status",
        help="查看配置与当前环境变量状态",
    )
    p_status.add_argument("--json", action="store_true", dest="json_output", help="输出 JSON")
    p_status.add_argument("--reveal", action="store_true", help="显示密码/密钥原文")

    p_list = sub.add_parser(
        "list",
        aliases=["profiles", "ls"],
        help="列出所有代理配置",
    )
    p_list.add_argument("--json", action="store_true", dest="json_output", help="输出 JSON")
    p_list.add_argument("--reveal", action="store_true", help="显示密码原文")

    p_show = sub.add_parser(
        "show",
        help="查看某个配置的详情",
    )
    p_show.add_argument("profile", nargs="?", metavar="配置名")
    p_show.add_argument("--reveal", action="store_true", help="显示密码/密钥原文")

    p_add = sub.add_parser(
        "add",
        help="新增一个代理配置",
        description=(
            "新增配置。代理地址可以是 http(s):// 或 socks5://；"
            "不写协议时默认 http://。"
        ),
    )
    p_add.add_argument("name", metavar="名称")
    p_add.add_argument("url", metavar="代理地址")
    p_add.add_argument("--https", metavar="URL", help="单独指定 HTTPS 代理")
    p_add.add_argument("--all", metavar="URL", dest="all_url", help="指定 all_proxy（如 SOCKS5）")
    p_add.add_argument(
        "--no-proxy",
        metavar="列表",
        help="逗号分隔的 no_proxy 列表；不指定时使用常用默认值",
    )
    p_add.add_argument(
        "--env",
        metavar="KEY=VALUE",
        action="append",
        default=[],
        help="额外环境变量，可重复使用",
    )
    p_add.add_argument("--use", action="store_true", help="同时把该配置设为当前配置")
    p_add.add_argument("--force", action="store_true", help="覆盖同名配置")

    p_remove = sub.add_parser(
        "remove",
        aliases=["rm", "delete"],
        help="删除一个配置",
    )
    p_remove.add_argument("name", metavar="名称")
    p_remove.add_argument("--force", action="store_true", help="允许删除当前配置")

    sub.add_parser(
        "edit",
        help="用 $EDITOR 编辑配置文件",
    )

    p_test = sub.add_parser(
        "test",
        help="测试代理端口/访问网页",
        description="默认测试各代理端口是否能连上；加 --http 或 --url 会通过代理实际访问网页。",
    )
    p_test.add_argument("profile", nargs="?", metavar="配置名")
    p_test.add_argument("--url", metavar="URL", help="通过代理访问该地址")
    p_test.add_argument(
        "--http",
        action="store_true",
        dest="use_http",
        help=f"通过代理访问默认测试地址 {DEFAULT_TEST_URL}",
    )
    p_test.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        metavar="秒",
        help="连接/请求超时，默认 5 秒",
    )
    p_test.add_argument("--insecure", action="store_true", help="HTTP 测试时忽略证书错误")

    p_exec = sub.add_parser(
        "exec",
        aliases=["run"],
        help="在代理环境下运行一条命令",
        description="不修改当前 shell，只给子进程设置代理环境变量。",
    )
    p_exec.add_argument("-p", "--profile", metavar="配置名", help="使用指定配置")
    p_exec.add_argument("argv", nargs=argparse.REMAINDER, metavar="命令")

    p_env = sub.add_parser(
        "env",
        help="输出当前配置的 shell 导出语句",
        description='可用于 eval "$(proxy-cli env)"；需要还原原环境时建议使用 shell 集成。',
    )
    p_env.add_argument("profile", nargs="?", metavar="配置名")

    p_init = sub.add_parser(
        "init",
        help="显示/安装 shell 集成",
        description="把 proxy() 函数写入 bash/zsh/fish 的 rc 文件。",
    )
    init_group = p_init.add_mutually_exclusive_group()
    init_group.add_argument("--install", action="store_true", help="写入 shell rc 文件")
    init_group.add_argument("--uninstall", action="store_true", help="从 shell rc 文件移除")
    p_init.add_argument(
        "--shell",
        choices=("auto", "bash", "zsh", "fish"),
        default="auto",
        help="目标 shell，默认自动识别",
    )
    p_init.add_argument("--cli-path", metavar="PATH", help=argparse.SUPPRESS)

    p_doctor = sub.add_parser(
        "doctor",
        help="检查安装、配置与 shell 集成",
    )
    p_doctor.add_argument("--network", action="store_true", help="同时测试代理端口连通性")

    p_path = sub.add_parser(
        "path",
        help="显示配置文件路径",
    )
    p_path.add_argument(
        "--ensure",
        action="store_true",
        help="配置文件不存在时创建默认配置",
    )
    sub.add_parser(
        "version",
        help="显示版本",
    )
    sub.add_parser(
        "help",
        help="显示帮助",
    )
    return parser


# ---------------------------------------------------------------------------
# emit-shell：真正修改当前 shell 的路径
# ---------------------------------------------------------------------------


def emit_dispatch(app: App, args: argparse.Namespace) -> int:
    command = args.command
    if command in ("on", "start", "enable"):
        return emit_on(app, args.profile)
    if command in ("stop", "off", "disable"):
        return emit_stop(app)
    if command == "toggle":
        if _active():
            return emit_stop(app)
        return emit_on(app, None)
    if command in ("use", "switch"):
        return emit_use(app, args.profile)
    if command in ("reload", "refresh"):
        return emit_reload(app)
    raise CliError(
        f"命令 {command!r} 不支持 --emit-shell；"
        f"只有 {', '.join(ENV_COMMANDS)} 需要 shell 集成。"
    )


def _activation_message(app: App, profile, *, switching: bool, temporary: bool) -> None:
    action = "已切换代理配置" if switching else "已启用代理配置"
    url = _fmt_value(profile.display_url())
    suffix = "（临时，未改变默认配置）" if temporary else ""
    app.say(app.ui.ok(f"{action} {profile.name!r} → {url}{suffix}", err=True), err=True)


def emit_on(app: App, profile_name: str | None) -> int:
    config = app.config(create=False)
    profile = config.get(profile_name)
    was_active = _active()
    code = render_activate(
        profile,
        current_vars=active_vars_from_env(),
        shell=app.shell_type,
    )
    _write_shell(code)
    _activation_message(
        app,
        profile,
        switching=was_active and os.environ.get(PROFILE_VAR) != profile.name,
        temporary=profile_name is not None,
    )
    return 0


def emit_stop(app: App) -> int:
    was_active = _active()
    code = render_deactivate(active_vars_from_env(), shell=app.shell_type)
    _write_shell(code)
    if was_active:
        app.say(app.ui.ok("已停用代理，环境变量已恢复", err=True), err=True)
    else:
        app.say(app.ui.info("代理未启用；已清理相关环境变量", err=True), err=True)
    return 0


def emit_use(app: App, profile_name: str) -> int:
    config = app.config(create=False)
    profile = config.get(profile_name)
    set_current(config, profile_name)
    code = render_activate(
        profile,
        current_vars=active_vars_from_env(),
        shell=app.shell_type,
    )
    _write_shell(code)
    app.say(
        app.ui.ok(
            f"已切换当前配置为 {profile.name!r} 并启用 → {_fmt_value(profile.display_url())}",
            err=True,
        ),
        err=True,
    )
    return 0


def emit_reload(app: App) -> int:
    config = app.config(create=False)
    was_active = _active()
    name = os.environ.get(PROFILE_VAR) if was_active else None
    if name and name not in config.profiles:
        app.say(
            app.ui.warn(
                f"当前 shell 使用的配置 {name!r} 已不存在，改用默认配置 {config.current!r}",
                err=True,
            ),
            err=True,
        )
        name = None
    profile = config.get(name)
    code = render_activate(
        profile,
        current_vars=active_vars_from_env(),
        shell=app.shell_type,
    )
    _write_shell(code)
    action = "已重新加载" if was_active else "已启用"
    app.say(
        app.ui.ok(
            f"{action}代理配置 {profile.name!r} → {_fmt_value(profile.display_url())}",
            err=True,
        ),
        err=True,
    )
    return 0


# ---------------------------------------------------------------------------
# 普通命令
# ---------------------------------------------------------------------------


def dispatch(app: App, args: argparse.Namespace) -> int:
    command = args.command
    if command in ("on", "start", "enable"):
        raise CliError(
            "`on` 需要 shell 集成才能修改当前终端的环境变量。\n"
            "请先运行 `proxy-cli init --install`，重新打开终端后直接使用 `proxy`。"
        )
    if command in ("stop", "off", "disable"):
        raise CliError(
            "`stop` 需要 shell 集成才能修改当前终端的环境变量。\n"
            "请先运行 `proxy-cli init --install`，重新打开终端后使用 `proxy stop`。"
        )
    if command == "toggle":
        raise CliError(
            "`toggle` 需要 shell 集成。\n"
            "请先运行 `proxy-cli init --install`，重新打开终端后使用 `proxy toggle`。"
        )
    if command in ("reload", "refresh"):
        raise CliError(
            "`reload` 需要 shell 集成。\n"
            "请先运行 `proxy-cli init --install`，重新打开终端后使用 `proxy reload`。"
        )
    if command in ("use", "switch"):
        return cmd_use_direct(app, args)
    if command == "status":
        return cmd_status(app, args)
    if command in ("list", "profiles", "ls"):
        return cmd_list(app, args)
    if command == "show":
        return cmd_show(app, args)
    if command == "add":
        return cmd_add(app, args)
    if command in ("remove", "rm", "delete"):
        return cmd_remove(app, args)
    if command == "edit":
        return cmd_edit(app, args)
    if command == "test":
        return cmd_test(app, args)
    if command in ("exec", "run"):
        return cmd_exec(app, args)
    if command == "env":
        return cmd_env(app, args)
    if command == "init":
        return cmd_init(app, args)
    if command == "doctor":
        return cmd_doctor(app, args)
    if command == "path":
        path = config_path(app.config_override)
        if args.ensure:
            ensure_config_file(app.config_override)
        print(path)
        return 0
    if command == "version":
        print(f"proxy-tool {__version__}")
        return 0
    raise CliError(f"未知命令: {command!r}")


def cmd_use_direct(app: App, args: argparse.Namespace) -> int:
    config = app.config(create=False)
    config.get(args.profile)
    set_current(config, args.profile)
    app.say(app.ui.ok(f"已把当前配置设为 {args.profile!r}；运行 `proxy` 启用"))
    return 0


def cmd_status(app: App, args: argparse.Namespace) -> int:
    config = app.config(create=False)
    active = _active()
    active_profile = os.environ.get(PROFILE_VAR)

    notes: list[str] = []
    if active and active_profile and active_profile not in config.profiles:
        notes.append(f"当前 shell 使用的配置 {active_profile!r} 已不存在")
        active_profile = None
    elif active and active_profile and active_profile != config.current:
        notes.append(
            f"当前 shell 使用 {active_profile!r}，默认 current 是 {config.current!r}；"
            f"运行 proxy 可切换回默认配置"
        )
    profile = config.get(active_profile if active and active_profile else None)
    expected = profile.proxy_env()
    current_vars = active_vars_from_env() if active else []
    managed = list(
        dict.fromkeys(list(BASE_ENV_VARS) + list(profile.env) + list(current_vars))
    )
    actual = {name: os.environ[name] for name in managed if name in os.environ}
    mismatch = [
        name
        for name in managed
        if expected.get(name) != os.environ.get(name)
    ] if active else []

    if args.json_output:
        payload = {
            "config": str(config.path),
            "current": config.current,
            "active": active,
            "active_profile": active_profile,
            "profile": profile.name,
            "proxy": {
                "http": _display_env("http_proxy", expected["http_proxy"], args.reveal)
                if "http_proxy" in expected
                else None,
                "https": _display_env("https_proxy", expected["https_proxy"], args.reveal)
                if "https_proxy" in expected
                else None,
                "all": _display_env("all_proxy", expected["all_proxy"], args.reveal)
                if "all_proxy" in expected
                else None,
                "no_proxy": expected.get("no_proxy"),
            },
            "env": {
                name: _display_env(name, value, args.reveal)
                for name, value in expected.items()
            },
            "actual": {
                name: _display_env(name, value, args.reveal)
                for name, value in actual.items()
            },
            "mismatch": mismatch,
            "notes": notes,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    app.ui.out(f"配置文件  {config.path}")
    app.ui.out(f"当前配置  {config.current}")
    if active:
        label = active_profile or profile.name
        app.ui.out(f"启用状态  {app.ui.ok(f'已启用（{label}）')}")
    else:
        app.ui.out(f"启用状态  {app.ui.warn('未启用（运行 proxy 启用）')}")
    if active and active_profile and active_profile != profile.name:
        app.ui.out(f"当前 shell  {active_profile}")

    app.ui.out("")
    app.ui.out("代理地址")
    resolved = profile.resolved()
    rows = (
        ("http_proxy", resolved.http),
        ("https_proxy", resolved.https),
        ("ftp_proxy", resolved.ftp),
        ("all_proxy", resolved.all),
        ("no_proxy", ",".join(profile.no_proxy) if profile.no_proxy else None),
    )
    for name, value in rows:
        shown = _fmt_value(value, args.reveal)
        app.ui.out(f"  {name:<12} {shown}")

    if profile.env:
        app.ui.out("")
        app.ui.out("额外环境变量")
        for name, value in profile.env.items():
            shown = _display_env(name, value, args.reveal)
            app.ui.out(f"  {name:<20} {shown}")

    if active:
        app.ui.out("")
        if mismatch:
            for name in mismatch:
                current = os.environ.get(name)
                current_text = _display_env(name, current, args.reveal) if current is not None else "（未设置）"
                expected_text = _display_env(name, expected[name], args.reveal) if name in expected else "（未设置）"
                app.ui.out(
                    app.ui.warn(
                        f"环境变量 {name} 与配置不一致：当前 {current_text}，配置 {expected_text}"
                    )
                )
        else:
            app.ui.out(app.ui.ok("当前 shell 环境与配置一致"))
    for note in notes:
        app.ui.out(app.ui.warn(note))
    return 0


def cmd_list(app: App, args: argparse.Namespace) -> int:
    config = app.config(create=False)
    rows = []
    for name, profile in config.profiles.items():
        rows.append(
            {
                "name": name,
                "current": name == config.current,
                "http": _fmt_json_url(profile.http, args.reveal),
                "https": _fmt_json_url(profile.https, args.reveal),
                "ftp": _fmt_json_url(profile.ftp, args.reveal),
                "all": _fmt_json_url(profile.all, args.reveal),
                "no_proxy": profile.no_proxy,
                "env": {
                    key: _display_env(key, value, args.reveal)
                    for key, value in profile.env.items()
                },
            }
        )
    if args.json_output:
        print(
            json.dumps(
                {"config": str(config.path), "current": config.current, "profiles": rows},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    app.ui.out(f"配置文件  {config.path}")
    app.ui.out(f"当前配置  {config.current}")
    app.ui.out("")
    width = max(len(name) for name in config.profiles)
    for name, profile in config.profiles.items():
        marker = "*" if name == config.current else " "
        url = _mask(profile.display_url(), args.reveal)
        extra = f"  (+{len(profile.env)} env)" if profile.env else ""
        line = f"  {marker} {name:<{width}}  {url}{extra}"
        if name == config.current:
            line = app.ui.bold(line)
        app.ui.out(line)
    app.ui.out("")
    app.ui.out(app.ui.dim("提示: proxy use <名称> 切换并启用；proxy show <名称> 查看详情"))
    return 0


def cmd_show(app: App, args: argparse.Namespace) -> int:
    config = app.config(create=False)
    profile = config.get(args.profile)
    active = _active()
    active_profile = os.environ.get(PROFILE_VAR)
    title = f"配置 {profile.name!r}"
    if profile.name == config.current:
        title += "（当前配置）"
    if active and active_profile == profile.name:
        title += "（当前 shell 已启用）"
    app.ui.out(app.ui.bold(title))
    app.ui.out("")
    for label, value in (
        ("http", profile.http),
        ("https", profile.https),
        ("ftp", profile.ftp),
        ("all", profile.all),
    ):
        app.ui.out(f"{label:<10} {_fmt_value(value, args.reveal)}")
    no_proxy = ",".join(profile.no_proxy) if profile.no_proxy else None
    app.ui.out(f"{'no_proxy':<10} {_fmt_value(no_proxy, args.reveal)}")
    if profile.env:
        app.ui.out("")
        app.ui.out("额外环境变量")
        for key, value in profile.env.items():
            app.ui.out(f"  {key:<20} {_display_env(key, value, args.reveal)}")
    app.ui.out("")
    app.ui.out(app.ui.dim(f"配置文件: {config.path}"))
    return 0


def _is_pristine_default(profile: Profile) -> bool:
    """判断配置是否仍是安装脚本生成的默认示例。"""
    return (
        profile.http == "http://127.0.0.1:7890"
        and profile.https == "http://127.0.0.1:7890"
        and profile.all is None
        and profile.ftp is None
        and not profile.env
        and profile.no_proxy == list(DEFAULT_NO_PROXY)
    )


def cmd_add(app: App, args: argparse.Namespace) -> int:
    name = args.name
    if not PROFILE_NAME_RE.match(name):
        raise CliError(
            f"配置名 {name!r} 不合法：只能使用字母、数字、下划线、点、横线，且以字母或数字开头"
        )

    config = app.config(create=True)
    existed = name in config.profiles
    only_pristine_default = (
        set(config.profiles) == {"default"}
        and config.current == "default"
        and _is_pristine_default(config.profiles["default"])
    )
    auto_use = (
        not existed
        and name != "default"
        and only_pristine_default
    )
    # 允许直接覆盖安装脚本生成的默认示例，避免第一次使用就要求 --force
    if existed and not args.force and not (name == "default" and only_pristine_default):
        raise CliError(f"配置 {name!r} 已存在；如需覆盖请加 --force")

    raw = args.url.strip()
    if "://" not in raw:
        raw = "http://" + raw
    try:
        scheme = urlsplit(raw).scheme.lower()
    except ValueError as exc:
        raise CliError(f"代理地址不合法: {args.url!r} ({exc})") from None
    if scheme not in PROXY_SCHEMES:
        raise CliError(f"不支持的代理协议: {scheme}")

    if scheme.startswith("socks"):
        all_url = normalize_proxy_url(raw, "all", name)
        http_url = None
        https_url = normalize_proxy_url(args.https, "https", name) if args.https else None
    else:
        http_url = normalize_proxy_url(raw, "http", name)
        https_url = (
            normalize_proxy_url(args.https, "https", name)
            if args.https
            else http_url
        )
        all_url = normalize_proxy_url(args.all_url, "all", name) if args.all_url else None

    env_values: dict[str, str] = {}
    for item in args.env:
        if "=" not in item:
            raise CliError(f"--env 需要 KEY=VALUE 形式: {item!r}")
        key, value = item.split("=", 1)
        env_values[key] = value
    env_values = normalize_env(env_values, name)

    if args.no_proxy is None:
        no_proxy = list(DEFAULT_NO_PROXY)
    else:
        no_proxy = normalize_no_proxy(args.no_proxy, name)

    profile = Profile(
        name=name,
        http=http_url,
        https=https_url,
        all=all_url,
        no_proxy=no_proxy,
        env=env_values,
    )
    config.profiles[name] = profile
    if args.use or auto_use:
        config.current = name
    save_config(config)

    action = "已更新" if existed else "已添加"
    app.ui.out(
        app.ui.ok(
            f"{action}配置 {name!r} → {_mask(profile.display_url(), False)}"
        )
    )
    if config.created:
        app.ui.out(app.ui.info(f"已创建配置文件: {config.path}"))
    if args.use:
        app.ui.out(app.ui.info("已设为当前配置；运行 `proxy` 让当前 shell 生效"))
    elif auto_use:
        app.ui.out(app.ui.info("已自动设为当前配置；运行 `proxy` 让当前 shell 生效"))
    elif existed and name == os.environ.get(PROFILE_VAR):
        app.ui.out(app.ui.warn("当前 shell 仍在使用旧值；运行 `proxy` 重新启用"))
    elif name == config.current:
        app.ui.out(app.ui.info("这是当前配置；运行 `proxy` 让它生效"))
    else:
        app.ui.out(app.ui.dim("提示: proxy use <名称> 切换并启用"))
    return 0


def cmd_remove(app: App, args: argparse.Namespace) -> int:
    config = app.config(create=False)
    name = args.name
    if name not in config.profiles:
        raise CliError(f"配置 {name!r} 不存在")
    if name == config.current and not args.force:
        raise CliError(
            f"{name!r} 是当前配置；请先用 `proxy use <其他配置>` 切换，或加 --force"
        )
    if len(config.profiles) == 1:
        raise CliError("至少要保留一个配置，不能删除最后一个")

    was_active = _active() and os.environ.get(PROFILE_VAR) == name
    del config.profiles[name]
    if config.current == name:
        config.current = next(iter(config.profiles))
    save_config(config)

    app.ui.out(app.ui.ok(f"已删除配置 {name!r}"))
    if config.current != name:
        app.ui.out(app.ui.info(f"当前配置: {config.current}"))
    if was_active:
        app.ui.out(app.ui.warn("当前 shell 仍在使用该配置；运行 `proxy stop` 或 `proxy` 更新"))
    return 0


def _pick_editor() -> list[str]:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if editor:
        parts = shlex.split(editor)
        if parts:
            return parts
    for candidate in ("editor", "nano", "vi", "vim"):
        path = shutil.which(candidate)
        if path:
            return [path]
    raise CliError("找不到编辑器；请设置 $EDITOR 或手动编辑配置文件")


def cmd_edit(app: App, args: argparse.Namespace) -> int:
    config_file, created = ensure_config_file(app.config_override)
    if created:
        app.ui.out(app.ui.info(f"已创建默认配置文件: {config_file}"))
    editor = _pick_editor()
    try:
        proc = subprocess.run([*editor, str(config_file)])
    except FileNotFoundError:
        raise CliError(f"找不到编辑器: {editor[0]}") from None
    if proc.returncode != 0:
        app.ui.out(app.ui.warn(f"编辑器退出码为 {proc.returncode}，继续检查配置"))
    try:
        load_config(app.config_override, create=False)
    except ConfigError as exc:
        raise CliError(f"编辑后的配置无效，请修复后重试:\n{exc}") from None
    app.ui.out(app.ui.ok(f"配置已保存: {config_file}"))
    app.ui.out(app.ui.dim("运行 `proxy` 让当前 shell 生效"))
    return 0


def cmd_test(app: App, args: argparse.Namespace) -> int:
    if args.timeout <= 0:
        raise CliError("--timeout 必须大于 0")
    config = app.config(create=False)
    profile = config.get(args.profile)
    endpoints = profile_endpoints(profile)
    if not endpoints:
        raise CliError(f"配置 {profile.name!r} 没有代理地址")

    app.ui.out(app.ui.bold(f"测试配置 {profile.name!r}"))
    all_ok = True
    for label, url in endpoints:
        result = tcp_test(url, timeout=args.timeout)
        shown = _mask(url, False)
        if result.ok:
            app.ui.out(
                app.ui.ok(
                    f"{label:<10} {shown}  {format_latency(result.latency_ms)}"
                )
            )
        else:
            all_ok = False
            app.ui.out(app.ui.bad(f"{label:<10} {shown}"))
            app.ui.out(f"           {result.error}")

    target = args.url
    if args.use_http and not target:
        target = DEFAULT_TEST_URL
    if target:
        app.ui.out("")
        app.ui.out(f"通过代理访问 {target}")
        try:
            result, proxy_url = http_test(
                profile,
                target,
                timeout=max(args.timeout, 5.0),
                insecure=args.insecure,
            )
        except NetError as exc:
            all_ok = False
            app.ui.out(app.ui.bad(str(exc)))
        else:
            if result.ok:
                app.ui.out(
                    app.ui.ok(
                        f"HTTP {result.status}  {format_latency(result.latency_ms)}"
                        f"  (via {_mask(proxy_url, False)}, {result.tool})"
                    )
                )
            else:
                all_ok = False
                app.ui.out(app.ui.bad(f"{result.error}"))

    app.ui.out("")
    if all_ok:
        app.ui.out(app.ui.ok("测试通过"))
        return 0
    app.ui.out(app.ui.bad("存在失败项"))
    return 1


def cmd_exec(app: App, args: argparse.Namespace) -> int:
    command = list(args.argv)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        raise CliError("用法: proxy exec [--profile 名称] -- <命令> [参数...]")

    config = app.config(create=False)
    profile = config.get(args.profile)
    values = profile.proxy_env()
    managed = list(
        dict.fromkeys(
            list(BASE_ENV_VARS) + active_vars_from_env() + list(values.keys())
        )
    )
    env = os.environ.copy()
    for name in managed:
        if name in values:
            env[name] = values[name]
        else:
            env.pop(name, None)
    env[ACTIVE_VAR] = "1"
    env[PROFILE_VAR] = profile.name
    env[VARS_VAR] = " ".join(managed)

    try:
        proc = subprocess.run(command, env=env)
    except FileNotFoundError:
        app.error(f"找不到命令: {command[0]}")
        return 127
    except PermissionError:
        app.error(f"没有执行权限: {command[0]}")
        return 126
    return 128 - proc.returncode if proc.returncode < 0 else proc.returncode


def cmd_env(app: App, args: argparse.Namespace) -> int:
    config = app.config(create=False)
    profile = config.get(args.profile)
    _write_shell(render_plain_env(profile, shell=app.shell_type))
    return 0


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except UnicodeDecodeError:
        raise CliError(f"{path} 不是 UTF-8 编码") from None
    except OSError as exc:
        raise CliError(f"无法读取 {path}: {exc}") from None


def _write_rc(path: Path, text: str) -> None:
    try:
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    except OSError:
        mode = 0o644
    try:
        atomic_write_text(path, text, mode=mode)
    except OSError as exc:
        raise CliError(f"无法写入 {path}: {exc}") from None


def resolve_cli_path(explicit: str | None = None) -> Path:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise CliError(f"proxy-cli 不存在或不是文件: {path}")
        return path.resolve()

    env_path = os.environ.get("PROXY_TOOL_CLI")
    if env_path:
        path = Path(env_path).expanduser()
        if path.is_file():
            return path.resolve()

    argv0 = Path(sys.argv[0])
    if argv0.name in {"proxy-cli", "proxy"} and argv0.is_file():
        return argv0.resolve()

    found = shutil.which("proxy-cli")
    if found:
        return Path(found).resolve()
    raise CliError(
        "找不到 proxy-cli 可执行文件。\n"
        "请先运行项目目录中的 ./install.sh，或通过 --cli-path 指定路径。"
    )


def cmd_init(app: App, args: argparse.Namespace) -> int:
    if args.uninstall:
        changed: list[Path] = []
        for path in existing_rc_paths():
            old = _read_text(path)
            if BLOCK_START not in old:
                continue
            new = remove_shell_block(old)
            if new != old:
                _write_rc(path, new)
                changed.append(path)
        if changed:
            for path in changed:
                app.ui.out(app.ui.ok(f"已从 {path} 移除 shell 集成"))
            app.ui.out(app.ui.dim("重新打开终端或 source 对应 rc 文件后生效"))
        else:
            app.ui.out(app.ui.info("没有找到已安装的 shell 集成块"))
        return 0

    if args.install:
        config_file, config_created = ensure_config_file(app.config_override)
        shell = args.shell
        if shell == "auto":
            shell = detect_shell()
        if shell not in {"bash", "zsh", "fish"}:
            raise CliError(
                "无法自动识别当前 shell（支持 bash / zsh / fish）。\n"
                "请使用 `proxy-cli init --install --shell bash`、`--shell zsh` "
                "或 `--shell fish`。"
            )

        cli_path = resolve_cli_path(args.cli_path)
        if not os.access(cli_path, os.X_OK):
            raise CliError(f"{cli_path} 没有可执行权限")
        rc_path = rc_path_for_shell(shell)
        old_text = _read_text(rc_path)
        block = shell_block(
            cli_path,
            cli_path.parent,
            shell="fish" if shell == "fish" else "posix",
        )
        new_text = install_shell_block(old_text, block)
        if new_text == old_text:
            app.ui.out(app.ui.ok(f"shell 集成已是最新: {rc_path}"))
        else:
            _write_rc(rc_path, new_text)
            app.ui.out(app.ui.ok(f"已写入 shell 集成: {rc_path}"))

        if config_created:
            app.ui.out(app.ui.info(f"已创建默认配置: {config_file}"))
        else:
            app.ui.out(app.ui.info(f"配置文件: {config_file}"))
        app.ui.out("")
        app.ui.out(app.ui.bold("下一步"))
        app.ui.out(f"  1. 编辑配置，填入你的代理地址: {config_file}")
        app.ui.out(f"  2. 重新加载 shell: source {shlex.quote(str(rc_path))}")
        app.ui.out("  3. 运行: proxy")
        app.ui.out("")
        app.ui.out(app.ui.dim("提示: proxy test --http 可测试代理是否可用"))
        return 0

    shell = args.shell
    if shell == "auto":
        shell = detect_shell() or "bash"
    try:
        cli_path = resolve_cli_path(args.cli_path)
        bin_dir = cli_path.parent
    except CliError:
        cli_path = Path("proxy-cli")
        bin_dir = Path.home() / ".local" / "bin"
    app.ui.out(
        shell_block(
            cli_path,
            bin_dir,
            shell="fish" if shell == "fish" else "posix",
        )
    )
    target = (
        "~/.config/fish/config.fish" if shell == "fish" else "~/.bashrc 或 ~/.zshrc"
    )
    app.ui.err(app.ui.dim(f"将以上内容加入 {target}，或运行 proxy-cli init --install"))
    return 0


def cmd_doctor(app: App, args: argparse.Namespace) -> int:
    checks: list[tuple[str, str]] = []

    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    checks.append(("ok", f"Python {version}"))

    cli_path: Path | None
    try:
        cli_path = resolve_cli_path()
        checks.append(("ok", f"可执行文件 {cli_path}"))
    except CliError as exc:
        cli_path = None
        checks.append(("bad", str(exc).splitlines()[0]))

    config: Config | None = None
    path = config_path(app.config_override)
    if not path.exists():
        checks.append(("bad", f"配置文件不存在: {path}"))
    else:
        try:
            config = app.config(create=False)
        except ConfigError as exc:
            checks.append(("bad", f"配置无效: {exc}"))
        else:
            checks.append(("ok", f"配置文件 {path}"))
            try:
                mode = stat.S_IMODE(path.stat().st_mode)
                if mode & 0o077:
                    checks.append(
                        (
                            "warn",
                            f"配置文件权限为 {mode:o}，其他用户可能可读；建议 chmod 600 {path}",
                        )
                    )
                else:
                    checks.append(("ok", f"配置文件权限 {mode:o}"))
            except OSError:
                checks.append(("warn", "无法读取配置文件权限"))
            checks.append(("ok", f"配置语法正确，当前配置 {config.current!r}"))

    found_rc = []
    for rc in existing_rc_paths():
        try:
            text = rc.read_text(encoding="utf-8")
        except OSError:
            continue
        if BLOCK_START in text:
            found_rc.append(rc)
    if found_rc:
        checks.append(("ok", "shell 集成: " + ", ".join(str(p) for p in found_rc)))
    else:
        checks.append(
            (
                "warn",
                "未检测到 shell 集成；运行 `proxy-cli init --install` 安装",
            )
        )

    if cli_path is not None:
        bindir = str(cli_path.parent)
        path_dirs = os.environ.get("PATH", "").split(os.pathsep)
        if bindir in path_dirs:
            checks.append(("ok", f"{bindir} 在 PATH 中"))
        else:
            checks.append(
                (
                    "warn",
                    f"{bindir} 不在 PATH 中（安装的 shell 函数会自动补上；"
                    f"直接运行 proxy-cli 时需要 PATH）",
                )
            )
        found = shutil.which("proxy-cli")
        if found is None:
            checks.append(("warn", "PATH 中找不到 proxy-cli"))
        elif Path(found).resolve() != cli_path:
            checks.append(
                (
                    "warn",
                    f"PATH 中的 proxy-cli ({found}) 与当前程序 ({cli_path}) 不同",
                )
            )
        else:
            checks.append(("ok", "proxy-cli 在 PATH 中"))

    if config is not None:
        if _active():
            checks.append(
                (
                    "ok",
                    f"当前 shell 已启用（配置 {os.environ.get(PROFILE_VAR) or '?'}）",
                )
            )
        else:
            checks.append(("warn", "当前 shell 未启用代理"))

        if args.network:
            profile = config.get()
            for label, url in profile_endpoints(profile):
                result = tcp_test(url, timeout=3.0)
                if result.ok:
                    checks.append(
                        (
                            "ok",
                            f"代理端口 {label} {_mask(url, False)} "
                            f"{format_latency(result.latency_ms)}",
                        )
                    )
                else:
                    checks.append(("bad", f"代理端口 {label} 连接失败: {result.error}"))

    app.ui.out(app.ui.bold(f"proxy-tool doctor (v{__version__})"))
    app.ui.out("")
    has_bad = False
    for status, text in checks:
        if status == "ok":
            app.ui.out(app.ui.ok(text))
        elif status == "warn":
            app.ui.out(app.ui.warn(text))
        else:
            has_bad = True
            app.ui.out(app.ui.bad(text))
    app.ui.out("")
    if has_bad:
        app.ui.out(app.ui.bad("发现需要修复的问题"))
        return 1
    app.ui.out(app.ui.ok("检查完成，没有发现致命问题"))
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if getattr(args, "no_color", False):
        os.environ["NO_COLOR"] = "1"

    ui = UI(sys.stdout, sys.stderr)
    app = App(
        config_override=args.config,
        emit_shell=args.emit_shell,
        shell_type=args.shell_type,
        ui=ui,
    )

    if args.command is None or args.command == "help":
        parser.print_help()
        return 0

    try:
        if args.emit_shell:
            return emit_dispatch(app, args)
        return dispatch(app, args)
    except (ConfigError, NetError, CliError) as exc:
        app.error(str(exc))
        return exc.exit_code if isinstance(exc, CliError) else 1
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except OSError:
            pass
        return 0
    except OSError as exc:
        app.error(f"系统错误: {exc}")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
