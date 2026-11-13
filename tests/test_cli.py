from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers import base_env, make_launcher, run_bash, run_cli  # noqa: F401

from proxy_tool.config import load_config


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_path_ensure_creates_config(self) -> None:
        proc = run_cli(["path", "--ensure"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        path = Path(proc.stdout.strip())
        self.assertTrue(path.exists())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(load_config(path).current, "default")

    def test_path_does_not_create(self) -> None:
        proc = run_cli(["path"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(Path(proc.stdout.strip()).exists())

    def test_add_list_show_status_remove(self) -> None:
        proc = run_cli(
            [
                "add",
                "work",
                "http://127.0.0.1:7890",
                "--env",
                "NPM_CONFIG_PROXY=http://127.0.0.1:7890",
            ],
            self.home,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

        proc = run_cli(
            ["add", "socks", "socks5://127.0.0.1:7891", "--use"],
            self.home,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

        proc = run_cli(["list", "--json"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertEqual(data["current"], "socks")
        self.assertEqual(len(data["profiles"]), 3)

        proc = run_cli(["status", "--json"], self.home)
        data = json.loads(proc.stdout)
        self.assertEqual(data["current"], "socks")
        self.assertFalse(data["active"])
        self.assertIsNone(data["proxy"]["http"])
        self.assertEqual(data["proxy"]["all"], "socks5://127.0.0.1:7891")

        proc = run_cli(["show", "work"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("NPM_CONFIG_PROXY", proc.stdout)

        proc = run_cli(["use", "work"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(run_cli(["status", "--json"], self.home).stdout)
        self.assertEqual(data["current"], "work")

        proc = run_cli(["remove", "socks"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = run_cli(["remove", "work"], self.home)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("当前配置", proc.stderr)

    def test_cannot_remove_last_profile(self) -> None:
        run_cli(["add", "only", "http://127.0.0.1:1"], self.home)
        run_cli(["remove", "default"], self.home)
        proc = run_cli(["remove", "only", "--force"], self.home)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("最后一个", proc.stderr)

    def test_add_validation(self) -> None:
        proc = run_cli(["add", "bad name", "http://127.0.0.1:1"], self.home)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("不合法", proc.stderr)

        proc = run_cli(
            ["add", "bad", "http://127.0.0.1:1", "--env", "HTTP_PROXY=x"],
            self.home,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("保留变量", proc.stderr)

        proc = run_cli(["add", "dup", "http://127.0.0.1:1"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = run_cli(["add", "dup", "http://127.0.0.1:2"], self.home)
        self.assertNotEqual(proc.returncode, 0)
        proc = run_cli(
            ["add", "dup", "http://127.0.0.1:2", "--force"],
            self.home,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_add_default_overwrites_pristine_sample(self) -> None:
        run_cli(["path", "--ensure"], self.home)
        proc = run_cli(["add", "default", "http://127.0.0.1:9999"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(run_cli(["status", "--json"], self.home).stdout)
        self.assertEqual(data["proxy"]["http"], "http://127.0.0.1:9999")

    def test_masking(self) -> None:
        proc = run_cli(
            ["add", "secret", "http://user:pass@127.0.0.1:7890"],
            self.home,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(run_cli(["status", "--json"], self.home).stdout)
        self.assertIn("***", data["proxy"]["http"])
        self.assertNotIn("pass", data["proxy"]["http"])
        data = json.loads(
            run_cli(["status", "--json", "--reveal"], self.home).stdout
        )
        self.assertIn("pass", data["proxy"]["http"])
        data = json.loads(run_cli(["list", "--json"], self.home).stdout)
        secret = next(p for p in data["profiles"] if p["name"] == "secret")
        self.assertIn("***", secret["http"])

    def test_emit_shell_and_bash_eval(self) -> None:
        run_cli(["path", "--ensure"], self.home)
        proc = run_cli(["--emit-shell", "on"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("export http_proxy=", proc.stdout)
        self.assertNotIn("已启用", proc.stdout)
        self.assertIn("已启用", proc.stderr)

        script = r"""
        eval "$(python3 -m proxy_tool --emit-shell on 2>/dev/null)"
        printf 'A|%s|%s|%s\n' "$PROXY_TOOL_ACTIVE" "$PROXY_TOOL_PROFILE" "$http_proxy"
        eval "$(python3 -m proxy_tool --emit-shell stop 2>/dev/null)"
        printf 'B|%s|%s\n' "${PROXY_TOOL_ACTIVE-UNSET}" "${http_proxy-UNSET}"
        """
        bash = run_bash(script, self.home)
        self.assertEqual(bash.returncode, 0, bash.stderr)
        lines = bash.stdout.strip().splitlines()
        self.assertEqual(lines[0], "A|1|default|http://127.0.0.1:7890")
        self.assertEqual(lines[1], "B|UNSET|UNSET")

    def test_emit_shell_missing_config(self) -> None:
        proc = run_cli(["--emit-shell", "on"], self.home)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertIn("配置文件不存在", proc.stderr)

    def test_exec(self) -> None:
        run_cli(
            ["add", "work", "http://127.0.0.1:7890", "--env", "NPM_CONFIG_PROXY=http://127.0.0.1:7890"],
            self.home,
        )
        proc = run_cli(
            [
                "exec",
                "-p",
                "work",
                "--",
                "sh",
                "-c",
                'printf "%s|%s|%s|%s" "$http_proxy" "$NPM_CONFIG_PROXY" "$PROXY_TOOL_ACTIVE" "$PROXY_TOOL_PROFILE"',
            ],
            self.home,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout,
            "http://127.0.0.1:7890|http://127.0.0.1:7890|1|work",
        )

        proc = run_cli(["exec", "--", "sh", "-c", "exit 7"], self.home)
        self.assertEqual(proc.returncode, 7)

        proc = run_cli(["exec", "--", "definitely-not-a-command"], self.home)
        self.assertEqual(proc.returncode, 127)
        self.assertIn("找不到命令", proc.stderr)

    def test_exec_socks_removes_http_proxy(self) -> None:
        run_cli(["add", "socks", "socks5://127.0.0.1:1080"], self.home)
        proc = run_cli(
            [
                "exec",
                "--",
                "sh",
                "-c",
                'printf "%s|%s" "${http_proxy-UNSET}" "$all_proxy"',
            ],
            self.home,
            extra_env={"http_proxy": "http://should-be-removed:1"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "UNSET|socks5://127.0.0.1:1080")

    def test_env_output(self) -> None:
        run_cli(["add", "work", "http://127.0.0.1:7890"], self.home)
        proc = run_cli(["env", "work"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("export http_proxy=", proc.stdout)
        self.assertNotIn("已", proc.stdout)
        self.assertEqual(proc.stderr, "")

    def test_init_install_and_uninstall(self) -> None:
        rc = self.home / ".bashrc"
        rc.write_text("# my bashrc\n", encoding="utf-8")
        launcher = make_launcher(self.home)
        proc = run_cli(
            [
                "init",
                "--install",
                "--shell",
                "bash",
                "--cli-path",
                str(launcher),
            ],
            self.home,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        text = rc.read_text(encoding="utf-8")
        self.assertIn("# my bashrc", text)
        self.assertIn("# >>> proxy-tool >>>", text)
        self.assertIn("proxy()", text)
        self.assertTrue((self.home / ".config/proxy-tool/config.toml").exists())

        proc = run_cli(
            [
                "init",
                "--install",
                "--shell",
                "bash",
                "--cli-path",
                str(launcher),
            ],
            self.home,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("已是最新", proc.stdout)
        self.assertEqual(text, rc.read_text(encoding="utf-8"))

        proc = run_cli(["init", "--uninstall"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("proxy-tool", rc.read_text(encoding="utf-8"))
        self.assertIn("# my bashrc", rc.read_text(encoding="utf-8"))

    def test_init_unknown_shell_still_creates_config(self) -> None:
        proc = run_cli(
            ["init", "--install", "--shell", "auto"],
            self.home,
            extra_env={"SHELL": "/bin/tcsh"},
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("无法自动识别", proc.stderr)
        self.assertTrue((self.home / ".config/proxy-tool/config.toml").exists())

    def test_emit_shell_fish(self) -> None:
        if not shutil.which("fish"):
            self.skipTest("fish 未安装")
        run_cli(["path", "--ensure"], self.home)
        proc = run_cli(
            ["--emit-shell", "--shell-type", "fish", "on"],
            self.home,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("set -gx http_proxy", proc.stdout)
        self.assertIn("true", proc.stdout.splitlines()[-1])
        self.assertNotIn("export http_proxy", proc.stdout)
        self.assertIn("已启用", proc.stderr)

        script = r"""
        source $HOME/act.fish
        printf 'A|%s|%s|%s\n' "$PROXY_TOOL_ACTIVE" "$PROXY_TOOL_PROFILE" "$http_proxy"
        source $HOME/deact.fish
        printf 'B|%s|%s\n' "$PROXY_TOOL_ACTIVE" "$http_proxy"
        """
        act = self.home / "act.fish"
        deact = self.home / "deact.fish"
        act.write_text(proc.stdout, encoding="utf-8")
        stop = run_cli(
            ["--emit-shell", "--shell-type", "fish", "stop"],
            self.home,
        )
        self.assertEqual(stop.returncode, 0, stop.stderr)
        deact.write_text(stop.stdout, encoding="utf-8")
        fish = subprocess.run(
            ["fish", "-c", script],
            env={**base_env(self.home), "HOME": str(self.home)},
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(fish.returncode, 0, fish.stdout + fish.stderr)
        lines = fish.stdout.strip().splitlines()
        self.assertEqual(lines[0], "A|1|default|http://127.0.0.1:7890")
        self.assertEqual(lines[1], "B||")

    def test_env_fish(self) -> None:
        run_cli(["add", "work", "http://127.0.0.1:7890"], self.home)
        proc = run_cli(["--shell-type", "fish", "env", "work"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("set -gx http_proxy", proc.stdout)
        self.assertNotIn("export http_proxy", proc.stdout)
        self.assertTrue(proc.stdout.rstrip().endswith("true"))

    def test_init_fish(self) -> None:
        if not shutil.which("fish"):
            self.skipTest("fish 未安装")
        fish_dir = self.home / ".config/fish"
        fish_dir.mkdir(parents=True, exist_ok=True)
        rc = fish_dir / "config.fish"
        rc.write_text("# my fish config\n", encoding="utf-8")
        launcher = make_launcher(self.home)
        proc = run_cli(
            ["init", "--install", "--shell", "fish", "--cli-path", str(launcher)],
            self.home,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        text = rc.read_text(encoding="utf-8")
        self.assertIn("# my fish config", text)
        self.assertIn("# >>> proxy-tool >>>", text)
        self.assertIn("function proxy", text)
        self.assertIn("--shell-type fish", text)

        fish = subprocess.run(
            [
                "fish",
                "-c",
                f"source {rc}; proxy; printf 'A|%s|%s\n' $PROXY_TOOL_ACTIVE $http_proxy; "
                "proxy stop",
            ],
            env={**base_env(self.home), "HOME": str(self.home)},
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(fish.returncode, 0, fish.stdout + fish.stderr)
        self.assertIn("A|1|http://127.0.0.1:7890", fish.stdout)

        proc = run_cli(["init", "--uninstall"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("proxy-tool", rc.read_text(encoding="utf-8"))

    def test_doctor(self) -> None:
        rc = self.home / ".bashrc"
        rc.write_text("", encoding="utf-8")
        launcher = make_launcher(self.home)
        run_cli(
            [
                "init",
                "--install",
                "--shell",
                "bash",
                "--cli-path",
                str(launcher),
            ],
            self.home,
        )
        proc = run_cli(
            ["doctor"],
            self.home,
            extra_env={"PROXY_TOOL_CLI": str(launcher)},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Python", proc.stdout)
        self.assertIn("shell 集成", proc.stdout)

    def test_edit_with_true_editor(self) -> None:
        proc = run_cli(["edit"], self.home, extra_env={"EDITOR": "/bin/true"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("配置已保存", proc.stdout)
        self.assertTrue((self.home / ".config/proxy-tool/config.toml").exists())

    def test_test_tcp(self) -> None:
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(5)
        port = server.getsockname()[1]
        try:
            run_cli(["add", "local", f"http://127.0.0.1:{port}"], self.home)
            proc = run_cli(["test", "--timeout", "1"], self.home)
        finally:
            server.close()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("测试通过", proc.stdout)

    def test_use_missing_profile(self) -> None:
        proc = run_cli(["use", "nope"], self.home)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("不存在", proc.stderr)


if __name__ == "__main__":
    unittest.main()
