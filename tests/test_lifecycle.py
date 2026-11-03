import unittest
from unittest.mock import AsyncMock, patch

from core import bot, lifecycle


class LifecycleHookTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for attribute in ("_startups", "_shutdowns"):
            patcher = patch.object(lifecycle, attribute, [])
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_hook_signatures_and_registration_order(self):
        calls = []
        application = object()

        @lifecycle.startup
        async def without_argument():
            calls.append("first")

        @lifecycle.startup
        async def with_argument(app):
            self.assertIs(app, application)
            calls.append("second")

        @lifecycle.startup
        async def with_varargs(*args):
            self.assertEqual(args, (application,))
            calls.append("third")

        lifecycle.startup(without_argument)
        await lifecycle.run_startup(application)
        self.assertEqual(calls, ["first", "second", "third"])

    async def test_failed_hook_does_not_block_following_hook(self):
        failing = AsyncMock(side_effect=ValueError("hook failed"))
        working = AsyncMock()
        lifecycle.shutdown(failing)
        lifecycle.shutdown(working)
        with self.assertLogs(lifecycle.logger, level="ERROR"):
            await lifecycle.run_shutdown()
        working.assert_awaited_once()
        lifecycle.clear_hooks()
        self.assertEqual(lifecycle.hook_names(), {"startup": [], "shutdown": []})

    async def test_strict_shutdown_still_calls_all_hooks_before_raising(self):
        lifecycle.shutdown(AsyncMock(side_effect=RuntimeError("failure")))
        working = AsyncMock()
        lifecycle.shutdown(working)
        with self.assertLogs(lifecycle.logger, level="ERROR"):
            with self.assertRaises(lifecycle.PluginShutdownError):
                await lifecycle.run_shutdown(strict=True)
        working.assert_awaited_once()

    async def test_bot_delegates_plugin_lifecycle(self):
        application = object()
        with patch.object(bot, "start_plugins", AsyncMock()) as start, patch.object(bot, "stop_plugins", AsyncMock()) as stop:
            await bot._on_startup(application)
            await bot._on_shutdown(application)
        start.assert_awaited_once_with(application)
        stop.assert_awaited_once_with(application)
