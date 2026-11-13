"""极简终端着色与状态符号。"""

from __future__ import annotations

import os
import sys
from typing import TextIO

_RESET = "\033[0m"
_CODES = {
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "cyan": "\033[36m",
}


def color_enabled(stream: TextIO) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


class UI:
    def __init__(self, stdout: TextIO = sys.stdout, stderr: TextIO = sys.stderr):
        self.stdout = stdout
        self.stderr = stderr
        self.stdout_color = color_enabled(stdout)
        self.stderr_color = color_enabled(stderr)

    def _paint(self, stream: TextIO, enabled: bool, style: str, text: str) -> str:
        if not enabled or style not in _CODES:
            return text
        return f"{_CODES[style]}{text}{_RESET}"

    def out(self, text: str = "") -> None:
        print(text, file=self.stdout)

    def err(self, text: str = "") -> None:
        print(text, file=self.stderr)

    def ok(self, text: str, *, err: bool = False) -> str:
        stream = self.stderr if err else self.stdout
        enabled = self.stderr_color if err else self.stdout_color
        return self._paint(stream, enabled, "green", f"✓ {text}")

    def bad(self, text: str, *, err: bool = False) -> str:
        stream = self.stderr if err else self.stdout
        enabled = self.stderr_color if err else self.stdout_color
        return self._paint(stream, enabled, "red", f"✗ {text}")

    def warn(self, text: str, *, err: bool = False) -> str:
        stream = self.stderr if err else self.stdout
        enabled = self.stderr_color if err else self.stdout_color
        return self._paint(stream, enabled, "yellow", f"! {text}")

    def info(self, text: str, *, err: bool = False) -> str:
        stream = self.stderr if err else self.stdout
        enabled = self.stderr_color if err else self.stdout_color
        return self._paint(stream, enabled, "cyan", f"• {text}")

    def bold(self, text: str, *, err: bool = False) -> str:
        stream = self.stderr if err else self.stdout
        enabled = self.stderr_color if err else self.stdout_color
        return self._paint(stream, enabled, "bold", text)

    def dim(self, text: str, *, err: bool = False) -> str:
        stream = self.stderr if err else self.stdout
        enabled = self.stderr_color if err else self.stdout_color
        return self._paint(stream, enabled, "dim", text)
