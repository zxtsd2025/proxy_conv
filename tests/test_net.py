from __future__ import annotations

import socket
import socketserver
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers import SRC  # noqa: F401

from proxy_tool.config import Profile
from proxy_tool.net import (
    http_test,
    profile_endpoints,
    split_proxy_url,
    tcp_test,
)


class FakeProxy(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class ProxyHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.request.recv(4096)
            if not chunk:
                break
            data += chunk
        self.request.sendall(
            b"HTTP/1.1 204 No Content\r\n"
            b"Content-Length: 0\r\n"
            b"Connection: close\r\n\r\n"
        )


class NetTests(unittest.TestCase):
    def test_split_proxy_url(self) -> None:
        self.assertEqual(
            split_proxy_url("socks5://127.0.0.1:1080"),
            ("socks5", "127.0.0.1", 1080),
        )
        self.assertEqual(
            split_proxy_url("http://127.0.0.1"),
            ("http", "127.0.0.1", 80),
        )

    def test_profile_endpoints_dedupe(self) -> None:
        profile = Profile(
            name="a",
            http="http://127.0.0.1:7890",
            https="http://127.0.0.1:7890",
            all="socks5://127.0.0.1:7891",
        )
        endpoints = profile_endpoints(profile)
        self.assertEqual(len(endpoints), 2)
        self.assertEqual(endpoints[0][0], "http/https")

    def test_tcp_test(self) -> None:
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(5)
        port = server.getsockname()[1]
        try:
            result = tcp_test(f"http://127.0.0.1:{port}", timeout=1)
        finally:
            server.close()
        self.assertTrue(result.ok)
        self.assertIsNotNone(result.latency_ms)

    def test_http_test_through_fake_proxy(self) -> None:
        server = FakeProxy(("127.0.0.1", 0), ProxyHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            profile = Profile(name="p", http=f"http://127.0.0.1:{port}")
            result, proxy_url = http_test(
                profile,
                "http://example.invalid/check",
                timeout=5,
            )
        finally:
            server.shutdown()
            server.server_close()
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.status, 204)
        self.assertEqual(proxy_url, f"http://127.0.0.1:{port}")


if __name__ == "__main__":
    unittest.main()
