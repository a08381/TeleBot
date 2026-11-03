import io
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core import dispatcher, runtime
from core.context import Context, _as_media


class CommandParsingTests(unittest.TestCase):
    def test_positional_and_keyword_arguments(self):
        self.assertEqual(
            dispatcher.parse_command("/check@ExampleBot hello rank=5 empty= value=a=b"),
            ("check", "ExampleBot", ["hello"], {"rank": "5", "empty": "", "value": "a=b"}),
        )

    def test_trailing_whitespace_and_multiline_arguments(self):
        self.assertEqual(dispatcher.parse_command("/ping \n"), ("ping", None, [], {}))
        self.assertEqual(
            dispatcher.parse_command("/check one\ntwo key=value"),
            ("check", None, ["one", "two"], {"key": "value"}),
        )

    def test_non_commands_are_ignored(self):
        for text in ("", "hello", "/", "/@ExampleBot", " /ping"):
            with self.subTest(text=text):
                self.assertIsNone(dispatcher.parse_command(text))


class DispatcherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for attribute in ("Listeners", "Buttons"):
            patcher = patch.object(dispatcher, attribute, {})
            patcher.start()
            self.addCleanup(patcher.stop)
        self.bot = SimpleNamespace(username="ExampleBot")
        self.callback_context = SimpleNamespace(bot=self.bot)

    async def test_case_insensitive_bot_mention_and_context_arguments(self):
        handler = AsyncMock()
        dispatcher.listener("CHECK")(handler)
        update = SimpleNamespace(effective_message=SimpleNamespace(text="/Check@examplebot one rank=5"))
        await dispatcher.dispatch_command(update, self.callback_context)
        handler.assert_awaited_once()
        ctx = handler.call_args.args[0]
        self.assertEqual(ctx.args, ["one"])
        self.assertEqual(ctx.kwargs, {"rank": "5"})
        self.assertEqual(handler.call_args.args[1:], ("one",))
        self.assertEqual(handler.call_args.kwargs, {"rank": "5"})
        ctx.args.append("mutated")
        ctx.kwargs["rank"] = "0"
        self.assertEqual(ctx.args, ["one"])
        self.assertEqual(ctx.kwargs["rank"], "5")

    async def test_other_bots_are_ignored(self):
        handler = AsyncMock()
        dispatcher.listener("ping")(handler)
        update = SimpleNamespace(effective_message=SimpleNamespace(text="/ping@OtherBot"))
        await dispatcher.dispatch_command(update, self.callback_context)
        handler.assert_not_awaited()

    async def test_command_during_reload_reports_busy_without_running_handler(self):
        handler = AsyncMock()
        dispatcher.listener("ping")(handler)
        message = SimpleNamespace(text="/ping", reply_text=AsyncMock())
        update = SimpleNamespace(effective_message=message, callback_query=None)
        async with runtime.exclusive_plugin_update():
            await dispatcher.dispatch_command(update, self.callback_context)
        handler.assert_not_awaited()
        message.reply_text.assert_awaited_once_with("插件正在重载，请稍后重试。", reply_markup=None)
        await dispatcher.dispatch_command(update, self.callback_context)
        handler.assert_awaited_once()

    async def test_callback_during_reload_reports_busy_without_running_handler(self):
        handler = AsyncMock()
        dispatcher.button("demo:")(handler)
        query = SimpleNamespace(data="demo:again")
        with patch.object(Context, "answer_query", new_callable=AsyncMock) as answer:
            async with runtime.exclusive_plugin_update():
                await dispatcher.dispatch_callback(SimpleNamespace(callback_query=query), self.callback_context)
        handler.assert_not_awaited()
        answer.assert_awaited_once_with("插件正在重载，请稍后重试。")

    async def test_plugin_failure_does_not_block_other_handlers(self):
        failing = AsyncMock(side_effect=ValueError("test failure"))
        working = AsyncMock()
        with self.assertLogs(dispatcher.logger, level="ERROR"):
            await dispatcher._run_all([failing, working], Context(self.bot, None))
        working.assert_awaited_once()

    def test_longest_literal_prefix_precedes_regex(self):
        short, long, regex = AsyncMock(), AsyncMock(), AsyncMock()
        dispatcher.button("demo:")(short)
        dispatcher.button("demo:item:")(long)
        dispatcher.button(r"demo:(.*)")(regex)
        self.assertEqual(dispatcher.match_button("demo:item:123"), ([long], ("123",)))

    def test_regex_groups_and_invalid_patterns(self):
        handler = AsyncMock()
        dispatcher.button("[")(AsyncMock())
        dispatcher.button(r"item:(\d+):(\w+)")(handler)
        self.assertEqual(dispatcher.match_button("item:12:view"), ([handler], ("12", "view")))
        self.assertEqual(dispatcher.match_button("other"), ([], ()))

    async def test_unknown_callback_is_answered(self):
        query = SimpleNamespace(data="missing", answer=AsyncMock())
        await dispatcher.dispatch_callback(SimpleNamespace(callback_query=query), self.callback_context)
        query.answer.assert_awaited_once_with("未知按钮")


class ContextTests(unittest.TestCase):
    def test_direct_context_has_empty_arguments(self):
        ctx = Context(None, None)
        self.assertEqual(ctx.args, [])
        self.assertEqual(ctx.kwargs, {})

    def test_local_media_handle_is_closed_after_reading(self):
        stream = io.BytesIO(b"media content")
        with patch.object(Path, "open", return_value=stream):
            media = _as_media(Path("sample.png"))
        self.assertTrue(stream.closed)
        self.assertEqual(media.input_file_content, b"media content")
        self.assertEqual(media.filename, "sample.png")
