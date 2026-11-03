import asyncio
import unittest

from core import runtime


class PluginExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_update_waits_for_existing_call_and_blocks_new_calls(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        events = []

        async def handler():
            async with runtime.plugin_execution():
                entered.set()
                await release.wait()
                events.append("handler finished")

        async def update():
            async with runtime.exclusive_plugin_update():
                events.append("reload")

        active = asyncio.create_task(handler())
        await entered.wait()
        reload = asyncio.create_task(update())
        try:
            await asyncio.sleep(0)
            self.assertFalse(reload.done())
            with self.assertRaises(runtime.PluginsReloading):
                async with runtime.plugin_execution():
                    self.fail("重载期间不应进入插件")
        finally:
            release.set()
            await asyncio.gather(active, reload)
        self.assertEqual(events, ["handler finished", "reload"])

    async def test_reload_from_current_handler_does_not_wait_for_itself(self):
        async def handler():
            async with runtime.plugin_execution():
                async with runtime.exclusive_plugin_update():
                    pass
        await asyncio.wait_for(handler(), timeout=1)
        self.assertEqual(runtime._active_calls, {})

    async def test_concurrent_reload_is_reported(self):
        async with runtime.exclusive_plugin_update():
            with self.assertRaises(runtime.PluginsReloading):
                async with runtime.exclusive_plugin_update():
                    self.fail("重载不应重入")

    async def test_old_dispatch_generation_is_rejected_after_reload(self):
        generation = runtime.plugin_generation()
        async with runtime.exclusive_plugin_update():
            pass
        with self.assertRaises(runtime.PluginsReloading):
            async with runtime.plugin_execution(generation):
                self.fail("不应执行旧处理器")
        async with runtime.plugin_execution(runtime.plugin_generation()):
            pass

    async def test_cancelling_reload_does_not_cancel_active_request(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def handler():
            async with runtime.plugin_execution():
                entered.set()
                await release.wait()

        async def update():
            async with runtime.exclusive_plugin_update():
                self.fail("当前请求尚未结束")

        active = asyncio.create_task(handler())
        await entered.wait()
        reload = asyncio.create_task(update())
        await asyncio.sleep(0)
        reload.cancel()
        try:
            with self.assertRaises(asyncio.CancelledError):
                await reload
            self.assertFalse(active.done())
            self.assertFalse(runtime._reloading)
        finally:
            release.set()
            await active

    async def test_nested_calls_release_activity_on_error(self):
        with self.assertRaises(ValueError):
            async with runtime.plugin_execution():
                async with runtime.plugin_execution():
                    raise ValueError("failure")
        self.assertEqual(runtime._active_calls, {})
