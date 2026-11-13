from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers import SRC, base_env  # noqa: F401

from proxy_tool.config import Profile
from proxy_tool.shell import (
    active_vars_from_env,
    install_shell_block,
    remove_shell_block,
    render_activate,
    render_deactivate,
    render_plain_env,
    shell_block,
)


class ShellCodeTests(unittest.TestCase):
    def run_bash(
        self,
        script: str,
        *,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as tmp:
            env = base_env(Path(tmp))
            if extra_env:
                env.update(extra_env)
            return subprocess.run(
                ["bash", "-c", script],
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )

    def test_activate_and_restore(self) -> None:
        profile = Profile(
            name="work",
            http="http://127.0.0.1:7890",
            no_proxy=["localhost", "*.local"],
            env={"NPM_CONFIG_PROXY": "http://127.0.0.1:7890"},
        )
        activate = render_activate(profile)
        deactivate = render_deactivate(["NPM_CONFIG_PROXY"])
        with tempfile.TemporaryDirectory() as tmp:
            act = Path(tmp) / "act.sh"
            deact = Path(tmp) / "deact.sh"
            act.write_text(activate, encoding="utf-8")
            deact.write_text(deactivate, encoding="utf-8")
            script = f"""
            unset HTTP_PROXY HTTPS_PROXY FTP_PROXY ALL_PROXY NO_PROXY all_proxy https_proxy ftp_proxy
            export http_proxy="OLD ' \\" $HOME value"
            export NO_PROXY="OLD-NO"
            export NPM_CONFIG_PROXY="OLD-NPM"
            . {str(act)!r}
            printf 'A|%s|%s|%s|%s|%s|%s\\n' "$PROXY_TOOL_ACTIVE" "$http_proxy" "$https_proxy" "${{all_proxy-UNSET}}" "$no_proxy" "$NPM_CONFIG_PROXY"
            . {str(deact)!r}
            printf 'B|%s|%s|%s|%s|%s|%s\\n' "${{PROXY_TOOL_ACTIVE-UNSET}}" "$http_proxy" "${{https_proxy-UNSET}}" "${{all_proxy-UNSET}}" "${{NO_PROXY-UNSET}}" "$NPM_CONFIG_PROXY"
            """
            proc = self.run_bash(script)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            lines = proc.stdout.strip().splitlines()
            self.assertTrue(lines[0].startswith("A|1|http://127.0.0.1:7890|http://127.0.0.1:7890|UNSET|localhost,*.local|http://127.0.0.1:7890"))
            self.assertTrue(lines[1].startswith("B|UNSET|OLD '"))
            self.assertIn("|UNSET|", lines[1])
            self.assertIn("|OLD-NO|OLD-NPM", lines[1])

    def test_activate_unsets_unconfigured_vars(self) -> None:
        profile = Profile(name="socks", all="socks5://127.0.0.1:1080")
        activate = render_activate(profile)
        with tempfile.TemporaryDirectory() as tmp:
            act = Path(tmp) / "act.sh"
            act.write_text(activate, encoding="utf-8")
            proc = self.run_bash(
                f'export http_proxy="old"; export https_proxy="old2"; . {str(act)!r}; '
                'printf "%s|%s|%s\\n" "${http_proxy-UNSET}" "${https_proxy-UNSET}" "$all_proxy"'
            )
            self.assertEqual(proc.stdout.strip(), "UNSET|UNSET|socks5://127.0.0.1:1080")

    def test_deactivate_without_saved_values_unsets(self) -> None:
        code = render_deactivate()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deact.sh"
            path.write_text(code, encoding="utf-8")
            proc = self.run_bash(
                f'export http_proxy="old"; . {str(path)!r}; '
                'printf "%s|%s\\n" "${http_proxy-UNSET}" "${PROXY_TOOL_ACTIVE-UNSET}"'
            )
            self.assertEqual(proc.stdout.strip(), "UNSET|UNSET")

    def test_profile_name_is_quoted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pwned = Path(tmp) / "pwned"
            profile = Profile(
                name=f"x'; touch {pwned}; echo '",
                http="http://127.0.0.1:1",
            )
            code = render_activate(profile)
            path = Path(tmp) / "act.sh"
            path.write_text(code, encoding="utf-8")
            proc = self.run_bash(f". {str(path)!r}; printf '%s' \"$PROXY_TOOL_PROFILE\"")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse(pwned.exists(), "profile name 被当成命令执行了")
            self.assertIn("touch", proc.stdout)

    def test_invalid_var_names_are_ignored(self) -> None:
        profile = Profile(name="a", http="http://127.0.0.1:1")
        code = render_activate(profile, current_vars=["BAD-NAME", "NPM_CONFIG_PROXY"])
        self.assertNotIn("BAD-NAME", code)
        self.assertIn("NPM_CONFIG_PROXY", code)

    def test_plain_env_has_no_save_blocks(self) -> None:
        code = render_plain_env(Profile(name="a", http="http://127.0.0.1:1"))
        self.assertNotIn("_PROXY_TOOL_SAVED_", code)
        self.assertIn("export http_proxy=", code)


@unittest.skipUnless(shutil.which("fish"), "fish 未安装")
class FishShellCodeTests(unittest.TestCase):
    def run_fish(self, script: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as tmp:
            env = base_env(Path(tmp))
            return subprocess.run(
                ["fish", "-c", script],
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )

    def test_activate_and_restore(self) -> None:
        profile = Profile(
            name="work",
            http="http://127.0.0.1:7890",
            no_proxy=["localhost", "*.local"],
            env={"NPM_CONFIG_PROXY": "http://127.0.0.1:7890"},
        )
        activate = render_activate(profile, shell="fish")
        deactivate = render_deactivate(["NPM_CONFIG_PROXY"], shell="fish")
        with tempfile.TemporaryDirectory() as tmp:
            act = Path(tmp) / "act.fish"
            deact = Path(tmp) / "deact.fish"
            act.write_text(activate, encoding="utf-8")
            deact.write_text(deactivate, encoding="utf-8")
            script = f"""
            set -gx http_proxy "OLD ' \\ $HOME value"
            set -gx HTTP_PROXY OLDUPPER
            set -gx NO_PROXY oldno
            set -gx NPM_CONFIG_PROXY OLDNPM
            set -e https_proxy
            set -e all_proxy
            set -e no_proxy
            source {str(act)!r}
            printf 'A|%s|%s|%s|%s|%s|%s\n' "$PROXY_TOOL_ACTIVE" "$PROXY_TOOL_PROFILE" "$PROXY_TOOL_PROFILE" "$http_proxy" "$https_proxy" "$NPM_CONFIG_PROXY"
            source {str(deact)!r}
            printf 'B|%s|%s|%s|%s|%s|%s\n' "$PROXY_TOOL_ACTIVE" "$http_proxy" "$HTTP_PROXY" "$https_proxy" "$no_proxy" "$NPM_CONFIG_PROXY"
            """
            proc = self.run_fish(script)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            lines = proc.stdout.strip().splitlines()
            self.assertTrue(lines[0].startswith("A|1|work|work|http://127.0.0.1:7890|http://127.0.0.1:7890|http://127.0.0.1:7890"))
            self.assertTrue(lines[1].startswith("B||OLD '"))
            self.assertIn("|OLDUPPER|", lines[1])
            self.assertIn("|OLDNPM", lines[1])

    def test_activate_unsets_unconfigured(self) -> None:
        profile = Profile(name="socks", all="socks5://127.0.0.1:1080")
        code = render_activate(profile, shell="fish")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "act.fish"
            path.write_text(code, encoding="utf-8")
            proc = self.run_fish(
                f'set -gx http_proxy old; source {str(path)!r}; '
                'printf "%s|%s|%s\\n" "$http_proxy" "$https_proxy" "$all_proxy"'
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), "||socks5://127.0.0.1:1080")

    def test_profile_name_is_quoted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pwned = Path(tmp) / "pwned"
            profile = Profile(
                name=f"x'; touch {pwned}; echo '",
                http="http://127.0.0.1:1",
            )
            code = render_activate(profile, shell="fish")
            path = Path(tmp) / "act.fish"
            path.write_text(code, encoding="utf-8")
            proc = self.run_fish(
                f'source {str(path)!r}; printf "%s" "$PROXY_TOOL_PROFILE"'
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse(pwned.exists())
            self.assertIn("touch", proc.stdout)

    def test_plain_env(self) -> None:
        code = render_plain_env(Profile(name="a", http="http://127.0.0.1:1"), shell="fish")
        self.assertIn("set -gx http_proxy", code)
        self.assertNotIn("_PROXY_TOOL_SAVED_", code)
        self.assertTrue(code.rstrip().endswith("true"))


class ShellBlockTests(unittest.TestCase):
    def test_install_is_idempotent(self) -> None:
        block = shell_block("/home/u/.local/bin/proxy-cli", "/home/u/.local/bin")
        original = "# my rc\n"
        once = install_shell_block(original, block)
        twice = install_shell_block(once, block)
        self.assertEqual(once, twice)
        self.assertIn("# my rc", twice)
        self.assertIn("proxy()", twice)
        self.assertIn("--emit-shell", twice)
        self.assertEqual(twice.count("# >>> proxy-tool >>>"), 1)

    def test_remove(self) -> None:
        block = shell_block("/x/proxy-cli", "/x")
        text = install_shell_block("# top\n", block)
        cleaned = remove_shell_block(text)
        self.assertNotIn("proxy-tool", cleaned)
        self.assertIn("# top", cleaned)

    @unittest.skipUnless(shutil.which("fish"), "fish 未安装")
    def test_fish_block_syntax(self) -> None:
        block = shell_block("/x/proxy-cli", "/x", shell="fish")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "block.fish"
            path.write_text(block, encoding="utf-8")
            proc = subprocess.run(
                ["fish", "-n", str(path)],
                capture_output=True,
                text=True,
                timeout=20,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("function proxy", block)
        self.assertIn("--shell-type fish", block)

    def test_active_vars_filters_injection(self) -> None:
        env = {"PROXY_TOOL_VARS": "http_proxy BAD-NAME NPM_CONFIG_PROXY $(touch /tmp/x)"}
        self.assertEqual(
            active_vars_from_env(env),
            ["http_proxy", "NPM_CONFIG_PROXY"],
        )


if __name__ == "__main__":
    unittest.main()
