"""配置文件的定位、解析、校验与写回。"""

from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlsplit

# 这些环境变量由 proxy-tool 统一管理。启用时会先保存旧值，停用时还原。
BASE_ENV_VARS: tuple[str, ...] = (
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

DEFAULT_NO_PROXY: tuple[str, ...] = ("localhost", "127.0.0.1", "::1", "*.local")

PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
HTTP_SCHEMES = {"http", "https"}
PROXY_SCHEMES = {"http", "https", "socks4", "socks4a", "socks5", "socks5h"}
DEFAULT_PORTS = {
    "http": 80,
    "https": 443,
    "socks4": 1080,
    "socks4a": 1080,
    "socks5": 1080,
    "socks5h": 1080,
}

DEFAULT_CONFIG_TEXT = """\
# proxy-tool 配置文件
# 修改后运行 `proxy` 即可把这里的代理写入当前终端。
# 支持多个配置：复制一个 [profiles.xxx] 段，然后 `proxy use xxx`。
# 文档与示例见项目 README。

current = "default"

[profiles.default]
# HTTP / HTTPS 代理地址（必填其一，或者填写 all）
http = "http://127.0.0.1:7890"
https = "http://127.0.0.1:7890"

# 如需 SOCKS5，取消下一行注释；
# 如果代理软件同时提供 HTTP 端口，通常保留上面的 http/https 即可。
# all = "socks5://127.0.0.1:7891"

# 不走代理的地址，逗号分隔（TOML 数组）。支持域名、IP、CIDR、通配符。
no_proxy = ["localhost", "127.0.0.1", "::1", "*.local"]

# 可选：强制给某些程序补充环境变量。例如：
# [profiles.default.env]
# NPM_CONFIG_PROXY = "http://127.0.0.1:7890"
# NPM_CONFIG_HTTPS_PROXY = "http://127.0.0.1:7890"
"""


class ConfigError(Exception):
    """配置相关错误，CLI 会把它转成友好的提示。"""


@dataclass
class Profile:
    name: str
    http: str | None = None
    https: str | None = None
    ftp: str | None = None
    all: str | None = None
    no_proxy: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)

    def resolved(self) -> "Profile":
        """只填了 http 时，https 默认复用 http。"""
        if self.https is None and self.http is not None:
            return replace(self, https=self.http)
        return self

    def proxy_env(self) -> dict[str, str]:
        """转换成要写入 shell 的环境变量（不含启用标记）。"""
        p = self.resolved()
        env: dict[str, str] = {}
        if p.http:
            env["http_proxy"] = p.http
            env["HTTP_PROXY"] = p.http
        if p.https:
            env["https_proxy"] = p.https
            env["HTTPS_PROXY"] = p.https
        if p.ftp:
            env["ftp_proxy"] = p.ftp
            env["FTP_PROXY"] = p.ftp
        if p.all:
            env["all_proxy"] = p.all
            env["ALL_PROXY"] = p.all
        if p.no_proxy:
            value = ",".join(p.no_proxy)
            env["no_proxy"] = value
            env["NO_PROXY"] = value
        env.update(p.env)
        return env

    def managed_vars(self) -> list[str]:
        """该配置会触碰的所有环境变量（含额外 env）。"""
        names = list(BASE_ENV_VARS)
        names.extend(self.env.keys())
        # 去重但保持顺序
        return list(dict.fromkeys(names))

    def display_url(self) -> str:
        for value in (self.http, self.all, self.https, self.ftp):
            if value:
                return value
        return "-"


@dataclass
class Config:
    path: Path
    current: str
    profiles: dict[str, Profile]
    created: bool = False

    def get(self, name: str | None = None) -> Profile:
        key = self.current if name is None else name
        try:
            return self.profiles[key]
        except KeyError:
            available = ", ".join(self.profiles) or "（无）"
            raise ConfigError(f"配置 {key!r} 不存在。可用配置: {available}") from None

    def names(self) -> list[str]:
        return list(self.profiles)


def config_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    """按优先级返回配置文件路径。"""
    if explicit:
        return Path(explicit).expanduser()

    env_path = os.environ.get("PROXY_CONFIG")
    if env_path:
        return Path(env_path).expanduser()

    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        base = Path(xdg).expanduser()
    else:
        base = Path.home() / ".config"
    return base / "proxy-tool" / "config.toml"


def _toml_str(value: str) -> str:
    """生成 TOML 基本字符串；JSON 的转义规则是 TOML 的子集。"""
    return json.dumps(value, ensure_ascii=False)


def normalize_proxy_url(value: object, field_name: str, profile_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(
            f"配置 {profile_name!r} 的 {field_name} 必须是字符串"
        )
    text = value.strip()
    if not text:
        return None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise ConfigError(f"配置 {profile_name!r} 的 {field_name} 含有控制字符")
    if "://" not in text:
        text = "http://" + text

    try:
        parts = urlsplit(text)
        port = parts.port  # 触发端口范围校验
    except ValueError as exc:
        raise ConfigError(
            f"配置 {profile_name!r} 的 {field_name} 不是合法的代理 URL: {value!r} ({exc})"
        ) from None

    scheme = parts.scheme.lower()
    if scheme not in PROXY_SCHEMES:
        allowed = ", ".join(sorted(PROXY_SCHEMES))
        raise ConfigError(
            f"配置 {profile_name!r} 的 {field_name} 使用了不支持的协议 {scheme!r}；支持: {allowed}"
        )
    if field_name in {"http", "https"} and scheme not in HTTP_SCHEMES:
        raise ConfigError(
            f"配置 {profile_name!r} 的 {field_name} 必须是 http:// 或 https:// 地址；"
            f"SOCKS 地址请写在 all 字段里"
        )
    if not parts.hostname:
        raise ConfigError(
            f"配置 {profile_name!r} 的 {field_name} 缺少主机名: {value!r}"
        )
    if any(ch.isspace() for ch in parts.hostname):
        raise ConfigError(
            f"配置 {profile_name!r} 的 {field_name} 主机名含有空白字符: {value!r}"
        )
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ConfigError(
            f"配置 {profile_name!r} 的 {field_name} 不应包含路径、查询串或片段: {value!r}"
        )
    # 端口 0 没有意义
    if port == 0:
        raise ConfigError(
            f"配置 {profile_name!r} 的 {field_name} 端口不能为 0: {value!r}"
        )
    return text


def normalize_no_proxy(value: object, profile_name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, list):
        items = value
    else:
        raise ConfigError(
            f"配置 {profile_name!r} 的 no_proxy 必须是字符串或字符串数组"
        )

    result: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise ConfigError(
                f"配置 {profile_name!r} 的 no_proxy 数组只能包含字符串"
            )
        for part in item.split(","):
            part = part.strip()
            if not part:
                continue
            if any(ord(ch) < 32 or ord(ch) == 127 for ch in part):
                raise ConfigError(
                    f"配置 {profile_name!r} 的 no_proxy 条目含有控制字符"
                )
            if part not in result:
                result.append(part)
    return result


def normalize_env(value: object, profile_name: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(
            f"配置 {profile_name!r} 的 env 必须是一个表，例如 [profiles.{profile_name}.env]"
        )
    result: dict[str, str] = {}
    for key, raw in value.items():
        if not isinstance(key, str) or not ENV_KEY_RE.match(key):
            raise ConfigError(
                f"配置 {profile_name!r} 的 env 变量名不合法: {key!r}"
            )
        if key in BASE_ENV_VARS or key.startswith(
            ("PROXY_TOOL_", "_PROXY_TOOL_", "_proxy_tool_")
        ):
            raise ConfigError(
                f"配置 {profile_name!r} 的 env 不能覆盖保留变量 {key!r}；"
                f"代理地址请直接写 http/https/all 字段"
            )
        if not isinstance(raw, str):
            raise ConfigError(
                f"配置 {profile_name!r} 的 env.{key} 必须是字符串"
            )
        if "\x00" in raw:
            raise ConfigError(
                f"配置 {profile_name!r} 的 env.{key} 含有 NUL 字符"
            )
        result[key] = raw
    return result


def parse_config_text(text: str, path: Path) -> Config:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"配置文件格式错误: {path}\n{exc}") from None

    if not isinstance(data, dict):
        raise ConfigError(f"配置文件格式错误: {path}")

    unknown_top = set(data) - {"current", "profiles"}
    if unknown_top:
        names = ", ".join(sorted(unknown_top))
        raise ConfigError(f"配置文件包含未知的顶层字段: {names}（{path}）")

    raw_profiles = data.get("profiles")
    if not isinstance(raw_profiles, dict) or not raw_profiles:
        raise ConfigError(f"配置文件中没有任何 [profiles.xxx] 段: {path}")

    profiles: dict[str, Profile] = {}
    for name, raw in raw_profiles.items():
        if not isinstance(name, str) or not PROFILE_NAME_RE.match(name):
            raise ConfigError(
                f"配置名不合法: {name!r}；只能使用字母、数字、下划线、点、横线"
            )
        if not isinstance(raw, dict):
            raise ConfigError(f"[profiles.{name}] 必须是一个表")

        allowed = {"http", "https", "ftp", "all", "no_proxy", "env"}
        unknown = set(raw) - allowed
        if unknown:
            fields = ", ".join(sorted(unknown))
            raise ConfigError(
                f"配置 {name!r} 包含未知字段: {fields}；允许的字段: "
                f"{', '.join(sorted(allowed))}"
            )

        profile = Profile(
            name=name,
            http=normalize_proxy_url(raw.get("http"), "http", name),
            https=normalize_proxy_url(raw.get("https"), "https", name),
            ftp=normalize_proxy_url(raw.get("ftp"), "ftp", name),
            all=normalize_proxy_url(raw.get("all"), "all", name),
            no_proxy=normalize_no_proxy(raw.get("no_proxy"), name),
            env=normalize_env(raw.get("env"), name),
        )
        if not any((profile.http, profile.https, profile.ftp, profile.all)):
            raise ConfigError(
                f"配置 {name!r} 至少需要 http、https、ftp、all 中的一个代理地址"
            )
        profiles[name] = profile

    current = data.get("current")
    if current is None or current == "":
        current = next(iter(profiles))
    if not isinstance(current, str):
        raise ConfigError("配置文件中的 current 必须是字符串")
    if current not in profiles:
        available = ", ".join(profiles)
        raise ConfigError(
            f"current = {current!r} 指向不存在的配置；可用配置: {available}"
        )

    return Config(path=path, current=current, profiles=profiles)


def ensure_config_file(
    explicit: str | os.PathLike[str] | None = None,
) -> tuple[Path, bool]:
    """确保配置文件存在；返回 (路径, 是否新建)。不解析已有配置。"""
    path = config_path(explicit)
    if path.exists():
        return path, False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, DEFAULT_CONFIG_TEXT, mode=0o600)
    except OSError as exc:
        raise ConfigError(f"无法创建配置文件 {path}: {exc}") from None
    return path, True


def load_config(
    explicit: str | os.PathLike[str] | None = None,
    *,
    create: bool = False,
) -> Config:
    """读取配置；``create=True`` 时若不存在则生成默认配置。"""
    path = config_path(explicit)
    created = False
    if not path.exists():
        if not create:
            raise ConfigError(
                f"配置文件不存在: {path}\n"
                f"运行 `proxy-cli init --install` 创建默认配置，"
                f"或运行 `proxy-cli add <名称> <代理地址>`。"
            )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, DEFAULT_CONFIG_TEXT, mode=0o600)
        except OSError as exc:
            raise ConfigError(f"无法创建配置文件 {path}: {exc}") from None
        created = True

    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ConfigError(f"配置文件不是 UTF-8 编码: {path}") from None
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件 {path}: {exc}") from None

    config = parse_config_text(text, path)
    config.created = created
    return config


def atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    """同目录临时文件 + os.replace，避免写一半损坏配置。

    如果目标是符号链接，写入链接指向的文件而不是把链接替换掉，
    这样 dotfiles 用户把 .bashrc / config.toml 软链到仓库时不会被破坏。
    """
    target = path.resolve() if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def config_to_text(config: Config) -> str:
    lines = [
        "# proxy-tool 配置文件",
        "# 由 proxy-tool 生成；可以手动编辑。",
        "# 修改后运行 `proxy` 重新把配置写入当前终端。",
        "",
        f"current = {_toml_str(config.current)}",
        "",
    ]
    for name, profile in config.profiles.items():
        lines.append(f"[profiles.{_toml_str(name)}]")
        for field_name in ("http", "https", "ftp", "all"):
            value = getattr(profile, field_name)
            if value:
                lines.append(f"{field_name} = {_toml_str(value)}")
        if profile.no_proxy:
            values = ", ".join(_toml_str(item) for item in profile.no_proxy)
            lines.append(f"no_proxy = [{values}]")
        if profile.env:
            lines.append("")
            lines.append(f"[profiles.{_toml_str(name)}.env]")
            for key, value in profile.env.items():
                lines.append(f"{key} = {_toml_str(value)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def save_config(config: Config) -> None:
    """完整写回配置（用于 add/remove 等结构变化）。"""
    if not config.profiles:
        raise ConfigError("至少需要保留一个配置")
    if config.current not in config.profiles:
        raise ConfigError(f"当前配置 {config.current!r} 不存在")
    try:
        atomic_write_text(config.path, config_to_text(config), mode=0o600)
    except OSError as exc:
        raise ConfigError(f"无法写入配置文件 {config.path}: {exc}") from None


def set_current(config: Config, name: str) -> None:
    """只改 current 行，尽量保留用户手写的注释。"""
    if name not in config.profiles:
        available = ", ".join(config.profiles)
        raise ConfigError(f"配置 {name!r} 不存在。可用配置: {available}")
    try:
        text = config.path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ConfigError(f"配置文件不是 UTF-8 编码: {config.path}") from None
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件 {config.path}: {exc}") from None

    new_line = f"current = {_toml_str(name)}"
    lines = text.splitlines()
    replaced = False
    for index, line in enumerate(lines):
        if re.match(r"^\s*current\s*=", line):
            lines[index] = new_line
            replaced = True
            break

    if replaced:
        new_text = "\n".join(lines).rstrip() + "\n"
    else:
        # 没有 current 行：插到第一个表头之前，保持 TOML 的顶层键顺序
        insert_at = 0
        for index, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("["):
                insert_at = index
                break
            insert_at = index + 1
        lines.insert(insert_at, new_line)
        new_text = "\n".join(lines).rstrip() + "\n"

    try:
        atomic_write_text(config.path, new_text, mode=0o600)
    except OSError as exc:
        raise ConfigError(f"无法写入配置文件 {config.path}: {exc}") from None
    config.current = name


def mask_url(value: str) -> str:
    """隐藏代理 URL 中的密码，用于展示。"""
    try:
        parts = urlsplit(value)
        if not parts.password:
            return value
        host = parts.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if parts.port:
            host = f"{host}:{parts.port}"
        user = parts.username or ""
        netloc = f"{user}:***@{host}" if user else f"***@{host}"
        return parts._replace(netloc=netloc).geturl()
    except ValueError:
        return value


def mask_env_value(key: str, value: str) -> str:
    """对可能含密钥的环境变量做脱敏。"""
    upper = key.upper()
    if any(word in upper for word in ("PASSWORD", "SECRET", "TOKEN", "API_KEY", "AUTH")):
        return "***"
    if "://" in value:
        return mask_url(value)
    return value
