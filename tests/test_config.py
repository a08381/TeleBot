import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils.config import BotSettings, TelegramSettings, WebhookSettings, load_config

plugin_module = importlib.import_module("utils.plugin_config")


class BotSettingsTests(unittest.TestCase):
    def test_defaults_do_not_grant_owner_permissions(self):
        settings = BotSettings.from_mapping({"telegram": {"token": "test-token"}})
        self.assertEqual(settings.owner_ids, ())
        self.assertFalse(settings.webhook.enable)

    def test_explicit_empty_owners_remain_empty(self):
        settings = BotSettings.from_mapping({"t_token": "test-token", "owner_ids": []})
        self.assertEqual(settings.owner_ids, ())

    def test_legacy_fields_are_supported(self):
        settings = BotSettings.from_mapping({
            "t_token": "legacy-token",
            "t_host": "https://api.example.test/",
            "owner_id": "123",
            "webhook": {"enable": True, "port": "8080"},
        })
        self.assertEqual(settings.token, "legacy-token")
        self.assertEqual(settings.host, "https://api.example.test")
        self.assertEqual(settings.owner_ids, (123,))
        self.assertEqual(settings.webhook.port, 8080)

    def test_telegram_section_takes_precedence(self):
        settings = BotSettings.from_mapping({
            "t_token": "old-token",
            "webhook": {"enable": True},
            "telegram": {"token": "new-token", "webhook": {"enable": False}},
        })
        self.assertEqual(settings.token, "new-token")
        self.assertFalse(settings.webhook.enable)

    def test_false_strings_are_not_truthy(self):
        for value in (False, "false", "0", "off", "no", 0):
            with self.subTest(value=value):
                self.assertFalse(WebhookSettings.from_mapping({"enable": value}).enable)
        settings = BotSettings.from_mapping({"token": "test", "log_split_streams": "false"})
        self.assertFalse(settings.log_split_streams)

    def test_invalid_boolean_is_reported(self):
        with self.assertRaisesRegex(ValueError, "布尔"):
            WebhookSettings.from_mapping({"enable": "not-a-boolean"})

    def test_missing_and_placeholder_tokens_are_reported(self):
        for token in (None, "", "你的Telegram Bot Token"):
            with self.subTest(token=token), self.assertRaises(ValueError):
                TelegramSettings.from_mapping({"token": token})

    def test_load_config_validates_json_root_and_caches(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            with patch("utils.config._config", None):
                path.write_text("[]", encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_config(path)
                path.write_text('{"token": "test"}', encoding="utf-8")
                settings = load_config(path)
                self.assertIs(load_config(), settings)


class PluginConfigTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config_dir = self.root / "config"
        for attribute, value in (("BASE_DIR", self.root), ("CONFIG_DIR", self.config_dir), ("_CACHE", {})):
            patcher = patch.object(plugin_module, attribute, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def make_config(self, defaults=None):
        return plugin_module.plugin_config(
            defaults or {"nested": {"tags": ["original"], "enabled": True}, "count": 3},
            name="demo",
        )

    def test_nested_defaults_are_added_and_unknown_fields_preserved(self):
        self.config_dir.mkdir()
        (self.config_dir / "demo.json").write_text(
            json.dumps({"nested": {"enabled": "false"}, "count": "7", "custom": [1]}),
            encoding="utf-8",
        )
        cfg = self.make_config()
        self.assertEqual(cfg.nested, {"tags": ["original"], "enabled": False})
        self.assertEqual(cfg.count, 7)
        self.assertEqual(cfg["custom"], [1])
        self.assertEqual(json.loads(cfg.path.read_text(encoding="utf-8"))["count"], 7)

    def test_reads_and_defaults_are_independent_copies(self):
        defaults = {"nested": {"tags": ["original"]}}
        cfg = self.make_config(defaults)
        defaults["nested"]["tags"].append("external")
        for value in (cfg["nested"], cfg.nested, cfg.get("nested"), cfg.as_dict()["nested"], cfg.defaults["nested"]):
            value["tags"].append("changed")
        self.assertEqual(cfg.nested, {"tags": ["original"]})
        self.assertEqual(cfg.defaults, {"nested": {"tags": ["original"]}})

    def test_writes_do_not_retain_caller_owned_objects(self):
        cfg = self.make_config()
        value = {"tags": ["new"]}
        cfg.set("nested", value, save=False)
        value["tags"].append("external")
        self.assertEqual(cfg.nested, {"tags": ["new"]})
        cfg.update({"nested": value}, save=False)
        value["tags"].append("again")
        self.assertEqual(cfg.nested, {"tags": ["new", "external"]})
        cfg.update(nested=value, save=False)
        value["tags"].append("last")
        self.assertEqual(cfg.nested, {"tags": ["new", "external", "again"]})

    def test_reset_does_not_modify_nested_defaults(self):
        cfg = self.make_config()
        cfg.set("nested", {"tags": ["changed"]}, save=False)
        cfg.reset(save=False)
        self.assertEqual(cfg.nested["tags"], ["original"])
        self.assertEqual(cfg.defaults["nested"]["tags"], ["original"])

    def test_invalid_scalar_falls_back_to_default(self):
        merged, changed = plugin_module._merge({"count": 3}, {"count": {"wrong": "type"}})
        self.assertEqual(merged, {"count": 3})
        self.assertTrue(changed)

    def test_reload_persists_missing_defaults(self):
        cfg = self.make_config()
        cfg.path.write_text('{"count": 9}', encoding="utf-8")
        cfg.reload()
        saved = json.loads(cfg.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["count"], 9)
        self.assertEqual(saved["nested"]["tags"], ["original"])

    def test_cache_and_explicit_creation_control(self):
        cfg = plugin_module.plugin_config({"x": 1}, name="cached", auto_create=False)
        self.assertFalse(cfg.path.exists())
        self.assertIs(cfg, plugin_module.plugin_config(name="cached"))
        plugin_module.clear_plugin_configs()
        self.assertIsNot(cfg, plugin_module.plugin_config(name="cached"))

    def test_nested_plugin_module_uses_root_package_config_name(self):
        namespace = {"__name__": "plugins.example.tools.settings"}
        exec("from utils.plugin_config import plugin_config\ncfg = plugin_config({'enabled': True})", namespace)
        self.assertEqual(namespace["cfg"].name, "example")
        self.assertEqual(namespace["cfg"].path, self.config_dir / "example" / "config.json")

    def test_empty_sanitized_name_uses_fallback(self):
        cfg = plugin_module.plugin_config({}, name="...")
        self.assertEqual(cfg.name, "plugin")
        self.assertEqual(cfg.path, self.config_dir / "plugin" / "config.json")

    def test_config_directory_can_be_created_without_a_config_file(self):
        directory = plugin_module.get_plugin_config_dir("demo", create=False)
        self.assertEqual(directory, self.config_dir / "demo")
        self.assertTrue(directory.is_absolute())
        self.assertFalse(directory.exists())
        self.assertEqual(plugin_module.get_plugin_config_dir("demo"), directory)
        self.assertTrue(directory.is_dir())
        self.assertFalse((directory / "config.json").exists())
        self.assertEqual(plugin_module.all_plugin_configs(), {})

    def test_directory_is_inferred_from_nested_plugin_and_exported_by_core(self):
        namespace = {"__name__": "plugins.example.tools.settings"}
        exec("from core import get_plugin_config_dir\ndirectory = get_plugin_config_dir()", namespace)
        self.assertEqual(namespace["directory"], self.config_dir / "example")
        self.assertTrue(namespace["directory"].is_dir())

    def test_directory_matches_config_and_does_not_depend_on_working_directory(self):
        with patch("os.getcwd", return_value=str(self.root / "elsewhere")):
            cfg = self.make_config()
            self.assertEqual(cfg.directory, plugin_module.get_plugin_config_dir("demo"))
            self.assertEqual(cfg.path, cfg.directory / "config.json")

    def test_directory_sanitizes_names(self):
        for name in ("../escape", "...", "../../", "hello/world"):
            with self.subTest(name=name):
                directory = plugin_module.get_plugin_config_dir(name, create=False)
                self.assertEqual(directory.parent, self.config_dir)

    def test_legacy_config_is_moved_and_migration_is_idempotent(self):
        self.config_dir.mkdir()
        legacy = self.config_dir / "demo.json"
        content = '{"count": 9, "custom": "keep"}'
        legacy.write_text(content, encoding="utf-8")
        cfg = self.make_config()
        self.assertEqual(cfg.path, self.config_dir / "demo" / "config.json")
        self.assertFalse(legacy.exists())
        self.assertEqual(cfg.count, 9)
        self.assertEqual(cfg["custom"], "keep")
        saved = cfg.path.read_bytes()
        plugin_module.clear_plugin_configs()
        self.assertEqual(self.make_config().count, 9)
        self.assertEqual(cfg.path.read_bytes(), saved)

    def test_existing_new_config_is_loaded_without_legacy_file(self):
        directory = plugin_module.get_plugin_config_dir("demo")
        (directory / "config.json").write_text('{"count": 12}', encoding="utf-8")
        self.assertEqual(self.make_config().count, 12)
        self.assertFalse((self.config_dir / "demo.json").exists())

    def test_config_migration_conflict_preserves_both_files(self):
        directory = plugin_module.get_plugin_config_dir("demo")
        legacy = self.config_dir / "demo.json"
        target = directory / "config.json"
        legacy.write_text('{"count": 1}', encoding="utf-8")
        target.write_text('{"count": 2}', encoding="utf-8")
        with self.assertRaisesRegex(FileExistsError, "新旧插件配置同时存在"):
            self.make_config()
        self.assertEqual(json.loads(legacy.read_text(encoding="utf-8")), {"count": 1})
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"count": 2})
        self.assertEqual(plugin_module.all_plugin_configs(), {})

    def test_config_created_during_migration_is_not_overwritten(self):
        from utils.plugin_migration import PluginDataMigrationError

        self.config_dir.mkdir()
        legacy = self.config_dir / "demo.json"
        target = self.config_dir / "demo" / "config.json"
        legacy.write_text('{"count": 1}', encoding="utf-8")
        link = os.link

        def concurrent_publish(source, destination):
            destination.write_text('{"count": 2}', encoding="utf-8")
            link(source, destination)

        with patch("utils.plugin_migration.os.link", side_effect=concurrent_publish):
            with self.assertRaises(PluginDataMigrationError):
                self.make_config()
        self.assertEqual(json.loads(legacy.read_text(encoding="utf-8")), {"count": 1})
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"count": 2})
        self.assertEqual(plugin_module.all_plugin_configs(), {})

    def test_invalid_legacy_config_is_preserved(self):
        self.config_dir.mkdir()
        legacy = self.config_dir / "demo.json"
        for content in ('{"count":', '[]'):
            with self.subTest(content=content):
                legacy.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.make_config()
                self.assertEqual(legacy.read_text(encoding="utf-8"), content)
                self.assertFalse((self.config_dir / "demo" / "config.json").exists())

    def test_auto_create_false_still_moves_an_existing_config_without_rewriting(self):
        self.config_dir.mkdir()
        legacy = self.config_dir / "demo.json"
        content = '{"count": 4}\n'
        legacy.write_text(content, encoding="utf-8")
        cfg = plugin_module.plugin_config({"count": 3, "extra": True}, name="demo", auto_create=False)
        self.assertFalse(legacy.exists())
        self.assertEqual(cfg.path.read_text(encoding="utf-8"), content)
        self.assertEqual(cfg.count, 4)
        self.assertTrue(cfg.extra)
