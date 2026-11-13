from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers import ROOT, base_env, run_bash  # noqa: F401


class InstallIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.env = base_env(self.home)
        (self.home / ".bashrc").write_text("# test bashrc\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def install(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(ROOT / "install.sh"), *args],
            cwd=str(ROOT),
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_install_use_and_uninstall(self) -> None:
        proc = self.install()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

        launcher = self.home / ".local/bin/proxy-cli"
        config = self.home / ".config/proxy-tool/config.toml"
        self.assertTrue(launcher.is_file())
        self.assertTrue(config.is_file())
        self.assertTrue(os.access(launcher, os.X_OK))
        rc_text = (self.home / ".bashrc").read_text(encoding="utf-8")
        self.assertIn("# >>> proxy-tool >>>", rc_text)
        self.assertIn("proxy()", rc_text)

        # 默认配置之外再添加一个 SOCKS 配置，并在真实 bash 中切换
        add = subprocess.run(
            [str(launcher), "add", "socks", "socks5://127.0.0.1:7891"],
            env=self.env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(add.returncode, 0, add.stderr)

        script = r"""
        set -u
        . "$HOME/.bashrc"
        type proxy >/dev/null
        proxy
        printf 'A|%s|%s|%s|%s\n' "$PROXY_TOOL_ACTIVE" "$PROXY_TOOL_PROFILE" "${http_proxy-UNSET}" "$all_proxy"
        proxy use default
        printf 'B|%s|%s|%s\n' "$PROXY_TOOL_PROFILE" "$http_proxy" "${all_proxy-UNSET}"
        proxy stop
        printf 'C|%s|%s|%s\n' "${PROXY_TOOL_ACTIVE-UNSET}" "${http_proxy-UNSET}" "${all_proxy-UNSET}"
        """
        bash = run_bash(script, self.home)
        self.assertEqual(bash.returncode, 0, bash.stdout + bash.stderr)
        lines = bash.stdout.strip().splitlines()
        self.assertEqual(lines[0], "A|1|socks|UNSET|socks5://127.0.0.1:7891")
        self.assertEqual(lines[1], "B|default|http://127.0.0.1:7890|UNSET")
        self.assertEqual(lines[2], "C|UNSET|UNSET|UNSET")

        doctor = subprocess.run(
            [str(launcher), "doctor"],
            env=self.env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)
        self.assertIn("shell 集成", doctor.stdout)

        uninstall = subprocess.run(
            [str(ROOT / "uninstall.sh"), "--purge"],
            cwd=str(ROOT),
            env=self.env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(uninstall.returncode, 0, uninstall.stdout + uninstall.stderr)
        self.assertFalse(launcher.exists())
        self.assertFalse((self.home / ".local/share/proxy-tool").exists())
        self.assertFalse(config.exists())
        self.assertNotIn("proxy-tool", (self.home / ".bashrc").read_text(encoding="utf-8"))

    def test_no_shell_install(self) -> None:
        before = (self.home / ".bashrc").read_text(encoding="utf-8")
        proc = self.install("--no-shell")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(before, (self.home / ".bashrc").read_text(encoding="utf-8"))
        self.assertTrue((self.home / ".config/proxy-tool/config.toml").exists())
        launcher = self.home / ".local/bin/proxy-cli"
        self.assertTrue(launcher.exists())

    @unittest.skipUnless(shutil.which("fish"), "fish 未安装")
    def test_fish_install(self) -> None:
        fish = shutil.which("fish")
        assert fish is not None
        self.env["SHELL"] = fish
        fish_dir = self.home / ".config/fish"
        fish_dir.mkdir(parents=True, exist_ok=True)
        rc = fish_dir / "config.fish"
        rc.write_text("# my fish config\n", encoding="utf-8")

        proc = self.install()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        text = rc.read_text(encoding="utf-8")
        self.assertIn("# my fish config", text)
        self.assertIn("# >>> proxy-tool >>>", text)
        self.assertIn("function proxy", text)
        self.assertIn("--shell-type fish", text)

        script = r"""
        source $HOME/.config/fish/config.fish
        proxy
        printf 'A|%s|%s|%s
' "$PROXY_TOOL_ACTIVE" "$PROXY_TOOL_PROFILE" "$http_proxy"
        proxy stop
        printf 'B|%s|%s
' "$PROXY_TOOL_ACTIVE" "$http_proxy"
        """
        fish_run = subprocess.run(
            [fish, "-c", script],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(fish_run.returncode, 0, fish_run.stdout + fish_run.stderr)
        lines = fish_run.stdout.strip().splitlines()
        self.assertEqual(lines[0], "A|1|default|http://127.0.0.1:7890")
        self.assertEqual(lines[1], "B||")

        doctor = subprocess.run(
            [str(self.home / ".local/bin/proxy-cli"), "doctor"],
            env=self.env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)
        self.assertIn("config.fish", doctor.stdout)

        uninstall = subprocess.run(
            [str(ROOT / "uninstall.sh"), "--purge"],
            cwd=str(ROOT),
            env=self.env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(uninstall.returncode, 0, uninstall.stdout + uninstall.stderr)
        self.assertNotIn("proxy-tool", rc.read_text(encoding="utf-8"))

    def test_custom_prefix(self) -> None:
        prefix = self.home / "opt/proxy-tool"
        proc = self.install("--prefix", str(prefix))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue((prefix / "bin/proxy-cli").exists())
        self.assertIn(
            str(prefix / "bin"),
            (self.home / ".bashrc").read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
