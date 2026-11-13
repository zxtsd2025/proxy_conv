"""代理连通性测试（TCP 端口 + 通过代理发起 HTTP 请求）。"""

from __future__ import annotations

import shutil
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from .config import DEFAULT_PORTS, mask_url

if TYPE_CHECKING:  # pragma: no cover
    from .config import Profile


class NetError(Exception):
    pass


@dataclass
class TcpResult:
    ok: bool
    latency_ms: float | None = None
    error: str | None = None


@dataclass
class HttpResult:
    ok: bool
    status: int | None = None
    latency_ms: float | None = None
    error: str | None = None
    tool: str = "curl"


def split_proxy_url(url: str) -> tuple[str, str, int]:
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise NetError(f"代理地址不合法: {url} ({exc})") from None
    scheme = parts.scheme.lower()
    host = parts.hostname
    if not host:
        raise NetError(f"代理地址缺少主机名: {url}")
    try:
        port = parts.port
    except ValueError as exc:
        raise NetError(f"代理端口不合法: {url} ({exc})") from None
    if port is None:
        port = DEFAULT_PORTS.get(scheme, 1080)
    return scheme, host, port


def tcp_test(url: str, timeout: float = 2.0) -> TcpResult:
    try:
        scheme, host, port = split_proxy_url(url)
    except NetError as exc:
        return TcpResult(False, error=str(exc))
    start = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            elapsed = (time.monotonic() - start) * 1000
            return TcpResult(True, latency_ms=elapsed)
    except OSError as exc:
        return TcpResult(
            False,
            error=f"{scheme}://{host}:{port} 连接失败: {exc}",
        )


def profile_endpoints(profile: "Profile") -> list[tuple[str, str]]:
    """按角色返回 (标签, URL)，相同 URL 合并标签。"""
    p = profile.resolved()
    roles = [("http", p.http), ("https", p.https), ("ftp", p.ftp), ("all", p.all)]
    by_url: dict[str, list[str]] = {}
    for label, url in roles:
        if url:
            by_url.setdefault(url, []).append(label)
    return [("/".join(labels), url) for url, labels in by_url.items()]


def _pick_proxy(profile: "Profile", target_scheme: str) -> str | None:
    p = profile.resolved()
    if target_scheme == "https":
        if p.https:
            return p.https
        if p.http:
            return p.http
    else:
        if p.http:
            return p.http
        if p.https:
            return p.https
    if p.all:
        return p.all
    return None


def _normalize_target(url: str) -> str:
    text = url.strip()
    if "://" not in text:
        text = "https://" + text
    try:
        parts = urlsplit(text)
    except ValueError as exc:
        raise NetError(f"测试地址不合法: {url} ({exc})") from None
    if parts.scheme not in {"http", "https"}:
        raise NetError("测试地址只支持 http:// 或 https://")
    if not parts.hostname:
        raise NetError(f"测试地址缺少主机名: {url}")
    return text


def _curl_http_test(
    proxy_url: str,
    target_url: str,
    timeout: float,
    insecure: bool,
) -> HttpResult:
    curl = shutil.which("curl")
    if not curl:
        raise FileNotFoundError
    command = [
        curl,
        "--silent",
        "--show-error",
        "--output",
        "/dev/null",
        "--noproxy",
        "",
        "--connect-timeout",
        str(max(1.0, timeout)),
        "--max-time",
        str(max(1.0, timeout)),
        "--write-out",
        "%{http_code} %{time_total}",
        "--proxy",
        proxy_url,
    ]
    if insecure:
        command.append("--insecure")
    command.append(target_url)

    start = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout + 2,
        )
    except subprocess.TimeoutExpired:
        return HttpResult(
            False,
            error=f"请求超时（{timeout:g}s）",
            latency_ms=(time.monotonic() - start) * 1000,
        )
    elapsed = (time.monotonic() - start) * 1000

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        message = detail[-1] if detail else f"curl 退出码 {proc.returncode}"
        return HttpResult(False, error=message, latency_ms=elapsed, tool="curl")

    fields = (proc.stdout or "").split()
    status: int | None = None
    if fields and fields[0].isdigit():
        status = int(fields[0])
    if status is None or status == 0:
        return HttpResult(False, error="没有收到 HTTP 响应", latency_ms=elapsed, tool="curl")
    return HttpResult(True, status=status, latency_ms=elapsed, tool="curl")


def _urllib_http_test(
    proxy_url: str,
    target_url: str,
    timeout: float,
    insecure: bool,
) -> HttpResult:
    target_scheme = urlsplit(target_url).scheme.lower()
    proxy_scheme = urlsplit(proxy_url).scheme.lower()
    if proxy_scheme.startswith("socks"):
        raise NetError(
            "系统没有 curl，且 Python 标准库不支持 SOCKS 代理测试；"
            "请安装 curl 或改用 HTTP 代理"
        )

    handler = urllib.request.ProxyHandler({target_scheme: proxy_url})
    context = ssl.create_default_context()
    if insecure:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    opener = urllib.request.build_opener(
        handler,
        urllib.request.HTTPSHandler(context=context),
    )
    request = urllib.request.Request(
        target_url,
        headers={"User-Agent": "proxy-tool/1.0 (+connectivity-test)"},
    )
    start = time.monotonic()
    try:
        with opener.open(request, timeout=timeout) as response:
            status = getattr(response, "status", None) or response.getcode()
            elapsed = (time.monotonic() - start) * 1000
            return HttpResult(True, status=status, latency_ms=elapsed, tool="urllib")
    except urllib.error.HTTPError as exc:
        # 能收到 HTTP 错误码，说明代理链路本身是通的
        elapsed = (time.monotonic() - start) * 1000
        return HttpResult(True, status=exc.code, latency_ms=elapsed, tool="urllib")
    except (urllib.error.URLError, OSError, ssl.SSLError) as exc:
        elapsed = (time.monotonic() - start) * 1000
        reason = getattr(exc, "reason", exc)
        return HttpResult(
            False,
            error=f"通过代理请求失败: {reason}",
            latency_ms=elapsed,
            tool="urllib",
        )


def _sanitize_error(message: str, proxy_url: str) -> str:
    """避免把代理 URL 里的密码带进错误信息。"""
    message = message.replace(proxy_url, mask_url(proxy_url))
    try:
        password = urlsplit(proxy_url).password
    except ValueError:
        password = None
    if password:
        message = message.replace(password, "***")
    return message


def http_test(
    profile: "Profile",
    target_url: str,
    *,
    timeout: float = 8.0,
    insecure: bool = False,
) -> tuple[HttpResult, str]:
    """通过 HTTP(S)/SOCKS 代理访问目标地址。返回 (结果, 使用的代理)。"""
    target = _normalize_target(target_url)
    target_scheme = urlsplit(target).scheme.lower()
    proxy_url = _pick_proxy(profile, target_scheme)
    if not proxy_url:
        raise NetError("当前配置没有可用于 HTTP 测试的代理地址")
    if shutil.which("curl"):
        result = _curl_http_test(proxy_url, target, timeout, insecure)
    else:
        result = _urllib_http_test(proxy_url, target, timeout, insecure)
    if result.error:
        result.error = _sanitize_error(result.error, proxy_url)
    return result, proxy_url


def format_latency(ms: float | None) -> str:
    if ms is None:
        return "-"
    if ms < 1000:
        return f"{ms:.0f} ms"
    return f"{ms / 1000:.1f} s"
