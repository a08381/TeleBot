import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from plugins import help as help_plugin


def handler(module="plugins.wm.wm", doc=None):
    async def callback(*args, **kwargs):
        raise AssertionError("生成帮助时不应执行业务指令")

    callback.__module__ = module
    callback.__doc__ = doc
    return callback


class HelpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = patch.dict(help_plugin.Listeners, {}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_current_registry_includes_aliases_and_deduplicates_handlers(self):
        callback = handler(doc="查询物品价格。\n\n内部实现细节。")
        help_plugin.Listeners.update({
            "price": [callback, callback], "wmprice": [callback], "empty": [],
        })
        text = "\n".join(help_plugin.command_help())
        self.assertIn("共 2 条", text)
        self.assertEqual(text.count("/price — 查询物品价格。"), 1)
        self.assertEqual(text.count("/wmprice — 查询物品价格。"), 1)
        self.assertNotIn("内部实现细节", text)
        self.assertNotIn("/empty", text)

    def test_new_and_removed_commands_are_reflected_immediately(self):
        help_plugin.Listeners["before"] = [handler()]
        first = "\n".join(help_plugin.command_help())
        help_plugin.Listeners.clear()
        help_plugin.Listeners["after"] = [handler()]
        second = "\n".join(help_plugin.command_help())
        self.assertIn("/before", first)
        self.assertNotIn("/before", second)
        self.assertIn("/after", second)

    def test_groups_and_commands_are_sorted_without_static_plugin_names(self):
        help_plugin.Listeners.update({
            "wmstatus": [handler()], "wmbump": [handler()],
            "ping": [handler("plugins.ping")],
            "new": [handler("plugins.future.commands")],
        })
        text = "\n".join(help_plugin.command_help())
        self.assertLess(text.index("【基础工具】"), text.index("【future】"))
        self.assertLess(text.index("【future】"), text.index("【wm】"))
        self.assertLess(text.index("/wmbump"), text.index("/wmstatus"))
        self.assertIn("/ping", text)
        self.assertIn("/new", text)

    def test_command_without_docstring_is_still_listed(self):
        help_plugin.Listeners["plain"] = [handler()]
        self.assertIn("/plain", "\n".join(help_plugin.command_help()).splitlines())

    def test_shared_command_combines_distinct_descriptions(self):
        help_plugin.Listeners["shared"] = [
            handler("plugins.first.commands", "第一个处理器。"),
            handler("plugins.second.commands", "第二个处理器。"),
        ]
        text = "\n".join(help_plugin.command_help())
        self.assertEqual(text.count("/shared"), 1)
        self.assertIn("第一个处理器。；第二个处理器。", text)
        self.assertIn("【first / second】", text)

    def test_empty_registry_has_clear_message(self):
        self.assertEqual(help_plugin.command_help(), ["当前没有已注册的指令。"])

    async def test_help_sends_plain_text_without_running_registered_commands(self):
        help_plugin.Listeners.update({
            "help": [help_plugin.help_command],
            "danger": [handler(doc="执行 <任务> & [参数]。")],
        })
        ctx = SimpleNamespace(reply_text=AsyncMock())
        await help_plugin.help_command(ctx)
        ctx.reply_text.assert_awaited_once()
        self.assertIsNone(ctx.reply_text.call_args.kwargs["parse_mode"])
        self.assertIn("/help", ctx.reply_text.call_args.args[0])
        self.assertIn("<任务> & [参数]", ctx.reply_text.call_args.args[0])

    async def test_many_commands_are_split_without_losing_any(self):
        help_plugin.Listeners.update({
            f"command_{number:03}": [handler(doc="🧩" * 100)]
            for number in range(100)
        })
        ctx = SimpleNamespace(reply_text=AsyncMock())
        await help_plugin.help_command(ctx)
        pages = [call.args[0] for call in ctx.reply_text.await_args_list]
        self.assertGreater(len(pages), 1)
        for page in pages:
            self.assertLessEqual(len(page.encode("utf-16-le")) // 2, help_plugin.MESSAGE_LIMIT)
        lines = "\n".join(pages).splitlines()
        self.assertEqual(sum(line.startswith("/command_") for line in lines), 100)

    def test_oversized_line_also_respects_utf16_limit(self):
        original = "🧩" * 6000
        pages = help_plugin._split_messages([original])
        self.assertEqual("".join(pages), original)
        self.assertTrue(all(0 < len(page.encode("utf-16-le")) // 2 <= help_plugin.MESSAGE_LIMIT for page in pages))


if __name__ == "__main__":
    unittest.main()
