from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers import SRC  # noqa: F401  (确保 src 在 sys.path 中)

from proxy_tool.config import (
    DEFAULT_CONFIG_TEXT,
    Config,
    ConfigError,
    Profile,
    config_path,
    load_config,
    mask_url,
    normalize_no_proxy,
    normalize_proxy_url,
    parse_config_text,
    save_config,
    set_current,
)


class ConfigParseTests(unittest.TestCase):
    def parse(self, text: str) -> Config:
        return parse_config_text(text, Path("/tmp/test-config.toml"))

    def test_default_config(self) -> None:
        config = self.parse(DEFAULT_CONFIG_TEXT)
        self.assertEqual(config.current, "default")
        profile = config.get()
        env = profile.proxy_env()
        self.assertEqual(env["http_proxy"], "http://127.0.0.1:7890")
        self.assertEqual(env["https_proxy"], env["http_proxy"])
        self.assertNotIn("all_proxy", env)
        self.assertIn("localhost", env["no_proxy"])

    def test_https_falls_back_to_http(self) -> None:
        config = self.parse(
            """
            current = "a"
            [profiles.a]
            http = "http://127.0.0.1:8080"
            """
        )
        profile = config.get()
        self.assertEqual(profile.https, None)
        self.assertEqual(profile.resolved().https, profile.http)

    def test_socks_only(self) -> None:
        config = self.parse(
            """
            [profiles.s]
            all = "socks5h://127.0.0.1:1080"
            """
        )
        self.assertEqual(config.current, "s")
        env = config.get().proxy_env()
        self.assertEqual(env["all_proxy"], "socks5h://127.0.0.1:1080")
        self.assertNotIn("http_proxy", env)

    def test_no_proxy_string_and_list(self) -> None:
        self.assertEqual(
            normalize_no_proxy("localhost, 127.0.0.1,,*.local", "p"),
            ["localhost", "127.0.0.1", "*.local"],
        )
        self.assertEqual(
            normalize_no_proxy(["localhost", "localhost", "::1"], "p"),
            ["localhost", "::1"],
        )

    def test_extra_env(self) -> None:
        config = self.parse(
            """
            [profiles.a]
            http = "http://127.0.0.1:8080"
            no_proxy = []

            [profiles.a.env]
            NPM_CONFIG_PROXY = "http://127.0.0.1:8080"
            """
        )
        env = config.get().proxy_env()
        self.assertEqual(env["NPM_CONFIG_PROXY"], "http://127.0.0.1:8080")
        self.assertNotIn("no_proxy", env)

    def test_missing_scheme_is_normalized(self) -> None:
        self.assertEqual(
            normalize_proxy_url("127.0.0.1:7890", "http", "p"),
            "http://127.0.0.1:7890",
        )

    def test_profile_name_with_dot(self) -> None:
        config = self.parse(
            """
            [profiles."home.lan"]
            http = "http://127.0.0.1:7890"
            """
        )
        self.assertIn("home.lan", config.profiles)

    def test_current_falls_back_to_first(self) -> None:
        config = self.parse(
            """
            [profiles.first]
            http = "http://127.0.0.1:1"
            [profiles.second]
            http = "http://127.0.0.1:2"
            """
        )
        self.assertEqual(config.current, "first")

    def test_invalid_current(self) -> None:
        with self.assertRaisesRegex(ConfigError, "current"):
            self.parse(
                """
                current = "missing"
                [profiles.a]
                http = "http://127.0.0.1:1"
                """
            )

    def test_socks_in_http_field_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "SOCKS"):
            self.parse(
                """
                [profiles.a]
                http = "socks5://127.0.0.1:1080"
                """
            )

    def test_unknown_profile_field(self) -> None:
        with self.assertRaisesRegex(ConfigError, "未知字段"):
            self.parse(
                """
                [profiles.a]
                http = "http://127.0.0.1:1"
                typo = "x"
                """
            )

    def test_reserved_env_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "保留变量"):
            self.parse(
                """
                [profiles.a]
                http = "http://127.0.0.1:1"
                [profiles.a.env]
                HTTP_PROXY = "http://x"
                """
            )

    def test_url_with_path_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "路径"):
            self.parse(
                """
                [profiles.a]
                http = "http://127.0.0.1:1/proxy"
                """
            )

    def test_mask_url(self) -> None:
        masked = mask_url("http://user:secret@127.0.0.1:7890")
        self.assertEqual(masked, "http://user:***@127.0.0.1:7890")
        self.assertEqual(mask_url("http://127.0.0.1:7890"), "http://127.0.0.1:7890")


class ConfigFileTests(unittest.TestCase):
    def test_save_and_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            config = Config(
                path=path,
                current="a",
                profiles={
                    "a": Profile(
                        name="a",
                        http="http://127.0.0.1:1",
                        no_proxy=["localhost"],
                        env={"NPM_CONFIG_PROXY": "http://127.0.0.1:1"},
                    ),
                    "b.c": Profile(name="b.c", all="socks5://127.0.0.1:2"),
                },
            )
            save_config(config)
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o600)
            loaded = load_config(path)
            self.assertEqual(loaded.current, "a")
            self.assertEqual(loaded.profiles["b.c"].all, "socks5://127.0.0.1:2")
            self.assertEqual(
                loaded.profiles["a"].env["NPM_CONFIG_PROXY"],
                "http://127.0.0.1:1",
            )

    def test_set_current_preserves_comments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                "# my comment\n"
                'current = "a"\n\n'
                '[profiles.a]\nhttp = "http://127.0.0.1:1"\n'
                '[profiles.b]\nhttp = "http://127.0.0.1:2"\n',
                encoding="utf-8",
            )
            config = load_config(path)
            set_current(config, "b")
            text = path.read_text(encoding="utf-8")
            self.assertIn("# my comment", text)
            self.assertIn('current = "b"', text)
            self.assertEqual(load_config(path).current, "b")

    def test_config_path_env_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("PROXY_CONFIG")
            try:
                os.environ["PROXY_CONFIG"] = str(Path(tmp) / "custom.toml")
                self.assertEqual(config_path(), Path(tmp) / "custom.toml")
            finally:
                if old is None:
                    os.environ.pop("PROXY_CONFIG", None)
                else:
                    os.environ["PROXY_CONFIG"] = old

    def test_create_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "config.toml"
            config = load_config(path, create=True)
            self.assertTrue(config.created)
            self.assertTrue(path.exists())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(config.current, "default")

    def test_atomic_write_preserves_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "dotfiles-config.toml"
            target.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8")
            link = Path(tmp) / "config.toml"
            link.symlink_to(target)
            config = load_config(link)
            set_current(config, "default")
            self.assertTrue(link.is_symlink())
            self.assertIn('current = "default"', target.read_text(encoding="utf-8"))

    def test_internal_env_prefix_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "保留变量"):
            parse_config_text(
                """
                [profiles.a]
                http = "http://127.0.0.1:1"
                [profiles.a.env]
                _PROXY_TOOL_SAVED_http_proxy = "x"
                """,
                Path("/tmp/test.toml"),
            )

    def test_invalid_toml_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.toml"
            path.write_text("not toml [", encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "格式错误"):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
