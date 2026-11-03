import importlib
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core import dispatcher, lifecycle, loader, webhook

plugin_config_module = importlib.import_module("utils.plugin_config")


class PackageLoaderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.package_name = "fixture_plugins"
        self.package = self.root / self.package_name
        self.package.mkdir()
        (self.package / "__init__.py").write_text("", encoding="utf-8")
        self.events = []
        patches = (
            patch.object(loader, "PLUGINS_PACKAGE", self.package_name),
            patch.object(loader, "PLUGINS_DIR", self.package),
            patch.object(loader, "_loaded", set()),
            patch.object(loader, "_started", False),
            patch.object(loader, "_application", None),
            patch.object(lifecycle, "_startups", []),
            patch.object(lifecycle, "_shutdowns", []),
            patch.object(webhook, "_ROUTES", {}),
            patch.object(plugin_config_module, "_CACHE", {}),
            patch.dict(dispatcher.Listeners, clear=True),
            patch.dict(dispatcher.Buttons, clear=True),
            patch.dict(sys.modules, {"fixture_events": SimpleNamespace(events=self.events)}),
            patch.object(sys, "path", [str(self.root), *sys.path]),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.remove_fixture_modules)

    def remove_fixture_modules(self):
        for name in list(sys.modules):
            if name == self.package_name or name.startswith(self.package_name + "."):
                sys.modules.pop(name, None)
        importlib.invalidate_caches()

    def write(self, name, content):
        target = self.package / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(content), encoding="utf-8")
        return target

    def make_plugin(self):
        self.write("demo/__init__.py", """
            def setup():
                from . import demo
        """)
        self.write("demo/demo.py", """
            from core import listener, startup, shutdown
            from fixture_events import events
            from .tools import value

            @listener("fixture")
            async def command(ctx, *args, **kwargs):
                return value.VALUE

            @startup
            async def start(application):
                events.append(("start", value.VALUE, application))

            @shutdown
            async def stop(application):
                events.append(("stop", value.VALUE, application))
        """)
        self.write("demo/tools/__init__.py", "")
        return self.write("demo/tools/value.py", "VALUE = 'old'\n")

    async def test_only_entrypoints_are_scanned_and_setup_is_idempotent(self):
        self.make_plugin()
        self.write("demo/unused.py", "raise AssertionError('不应自动导入未引用的工具')")
        self.write("_private.py", "raise AssertionError('不应扫描私有顶层文件')")
        self.write("single.py", "VALUE = 1")
        expected = ["fixture_plugins.demo", "fixture_plugins.single"]
        self.assertEqual(loader.iter_plugin_modules(), expected)
        self.assertEqual(loader.load_all_plugins(), expected)
        self.assertEqual(loader.load_all_plugins(), expected)
        self.assertEqual(len(dispatcher.Listeners["fixture"]), 1)
        self.assertNotIn("fixture_plugins.demo.unused", sys.modules)
        await loader.unload_all_plugins()
        self.assertFalse(any(name.startswith("fixture_plugins") for name in sys.modules))

    async def test_async_setup_is_reported_without_registering_plugin(self):
        self.write("demo/__init__.py", "async def setup(): pass")
        with self.assertLogs(loader.logger, level="ERROR") as logs:
            self.assertEqual(loader.load_all_plugins(), [])
        self.assertIn("setup 必须是同步函数", "\n".join(logs.output))
        self.assertEqual(loader._loaded, set())

    async def test_setup_returning_coroutine_is_reported_and_closed(self):
        self.write("demo/__init__.py", """
            async def initialize(): pass
            pending = initialize()
            def setup():
                return pending
        """)
        with self.assertLogs(loader.logger, level="ERROR") as logs:
            self.assertEqual(loader.load_all_plugins(), [])
        self.assertIn("setup 不应返回协程", "\n".join(logs.output))
        self.assertIsNone(sys.modules["fixture_plugins.demo"].pending.cr_frame)
        self.assertEqual(loader._loaded, set())

    async def test_reload_replaces_helpers_and_restarts_resources_with_same_app(self):
        helper_path = self.make_plugin()
        loader.load_all_plugins()
        application = object()
        await loader.start_plugins(application)
        old_module = sys.modules["fixture_plugins.demo.tools.value"]
        old_stat = helper_path.stat()
        helper_path.write_text("VALUE = 'new'\n", encoding="utf-8")
        os.utime(helper_path, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
        await loader.reload_all_plugins()
        new_module = sys.modules["fixture_plugins.demo.tools.value"]
        self.assertIsNot(new_module, old_module)
        self.assertEqual(new_module.VALUE, "new")
        self.assertEqual(self.events, [
            ("start", "old", application), ("stop", "old", application), ("start", "new", application)
        ])
        await loader.unload_all_plugins()
        self.assertEqual(self.events[-1], ("stop", "new", application))
        self.assertFalse(loader._started)
        self.assertFalse(dispatcher.Listeners)

    async def test_import_only_reload_does_not_start_resources(self):
        self.make_plugin()
        loader.load_all_plugins()
        await loader.reload_all_plugins()
        self.assertEqual(self.events, [])

    async def test_failed_shutdown_preserves_old_modules(self):
        self.make_plugin()
        loader.load_all_plugins()
        await loader.start_plugins()
        old_module = sys.modules["fixture_plugins.demo.tools.value"]

        @lifecycle.shutdown
        async def fail():
            raise RuntimeError("stop failed")

        with self.assertLogs(lifecycle.logger, level="ERROR"):
            with self.assertRaises(lifecycle.PluginShutdownError):
                await loader.reload_all_plugins()
        self.assertIs(sys.modules["fixture_plugins.demo.tools.value"], old_module)
        self.assertIn("fixture", dispatcher.Listeners)
        self.assertTrue(loader._started)
