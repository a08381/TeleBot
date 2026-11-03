import asyncio
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from core import runtime, server, webhook
from core.server import _application_lifespan, create_webhook_app
from utils.config import WebhookSettings


def fake_application(events):
    application = SimpleNamespace(running=False)

    def callback(name):
        async def record(*args):
            events.append(name)
            if name == "start":
                application.running = True
            elif name == "stop":
                application.running = False
        return AsyncMock(side_effect=record)

    for name in ("initialize", "post_init", "start", "stop", "post_stop", "shutdown", "post_shutdown"):
        setattr(application, name, callback(name))
    application.bot = SimpleNamespace(set_webhook=callback("set_webhook"))
    application.update_queue = asyncio.Queue()
    return application


class WebhookRequestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        patcher = patch.object(webhook, "_ROUTES", {})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.application = fake_application([])
        self.settings = WebhookSettings(
            enable=True, url="https://bot.example.test", path="/telegram", secret_token="test-secret"
        )
        self.handler = AsyncMock(return_value=web.Response(text="first"))
        webhook.webhook_route("/plugin/callback")(self.handler)
        app = create_webhook_app(self.application, self.settings)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.headers = {"X-Telegram-Bot-Api-Secret-Token": "test-secret"}

    async def test_valid_update_is_queued(self):
        response = await self.client.post("/telegram", json={"update_id": 123}, headers=self.headers)
        self.assertEqual(response.status, 200)
        update = self.application.update_queue.get_nowait()
        self.assertEqual(update.update_id, 123)

    async def test_secret_is_required_before_parsing(self):
        response = await self.client.post("/telegram", json={"update_id": 123})
        self.assertEqual(response.status, 401)
        self.assertTrue(self.application.update_queue.empty())

    async def test_non_json_is_rejected(self):
        response = await self.client.post("/telegram", data="hello", headers=self.headers)
        self.assertEqual(response.status, 415)

    async def test_malformed_updates_are_rejected(self):
        for payload in ([], {}, None, {"update_id": True}, {"update_id": "123"}, {"update_id": 1, "message": "invalid"}):
            with self.subTest(payload=payload):
                response = await self.client.post(
                    "/telegram", data=json.dumps(payload),
                    headers={**self.headers, "Content-Type": "application/json"},
                )
                self.assertEqual(response.status, 400)
        response = await self.client.post(
            "/telegram", data="{", headers={**self.headers, "Content-Type": "application/json"}
        )
        self.assertEqual(response.status, 400)
        self.assertTrue(self.application.update_queue.empty())

    async def test_plugin_route_does_not_require_telegram_secret(self):
        response = await self.client.get("/plugin/callback")
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.text(), "first")
        self.handler.assert_awaited_once()

    async def test_reload_returns_503_for_plugin_routes_but_still_queues_updates(self):
        async with runtime.exclusive_plugin_update():
            response = await self.client.get("/plugin/callback")
            self.assertEqual(response.status, 503)
            self.assertEqual(response.headers["Retry-After"], "1")
            self.handler.assert_not_awaited()
            response = await self.client.post("/telegram", json={"update_id": 123}, headers=self.headers)
            self.assertEqual(response.status, 200)
            self.assertEqual(self.application.update_queue.get_nowait().update_id, 123)
        response = await self.client.get("/plugin/callback")
        self.assertEqual(response.status, 200)
        self.handler.assert_awaited_once()

    async def test_existing_route_uses_reloaded_handler_and_removed_route_returns_404(self):
        webhook.clear_webhook_routes()
        webhook.webhook_route("/plugin/callback")(AsyncMock(return_value=web.Response(text="reloaded")))
        response = await self.client.get("/plugin/callback")
        self.assertEqual(await response.text(), "reloaded")
        webhook.clear_webhook_routes()
        response = await self.client.get("/plugin/callback")
        self.assertEqual(response.status, 404)

    def test_conflicting_path_is_reported(self):
        with self.assertRaisesRegex(ValueError, "冲突"):
            create_webhook_app(self.application, replace(self.settings, path="plugin/callback"))

    def test_invalid_public_url_and_port_are_reported(self):
        invalid = (
            {"url": "http://example.test"}, {"url": "https://"},
            {"url": "https://example.test?query=1"}, {"url": "https://example.test/#fragment"},
            {"port": 0}, {"port": 65536},
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                create_webhook_app(self.application, replace(self.settings, **values))


class ApplicationLifespanTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_and_shutdown_order(self):
        events = []
        application = fake_application(events)
        async with _application_lifespan(application):
            events.append("serve")
        self.assertEqual(events, [
            "initialize", "post_init", "start", "serve", "stop", "post_stop", "shutdown", "post_shutdown"
        ])

    async def test_partial_startup_still_cleans_up(self):
        events = []
        application = fake_application(events)
        application.post_init = AsyncMock(side_effect=RuntimeError("startup failed"))
        with self.assertRaisesRegex(RuntimeError, "startup failed"):
            async with _application_lifespan(application):
                self.fail("启动失败后不应进入服务阶段")
        self.assertEqual(events, ["initialize", "shutdown", "post_shutdown"])

    async def test_stop_failure_still_runs_shutdown(self):
        events = []
        application = fake_application(events)
        application.stop = AsyncMock(side_effect=RuntimeError("stop failed"))
        with self.assertRaisesRegex(RuntimeError, "stop failed"):
            async with _application_lifespan(application):
                pass
        self.assertEqual(events[-2:], ["shutdown", "post_shutdown"])

    async def test_shutdown_failure_still_runs_post_shutdown(self):
        events = []
        application = fake_application(events)
        application.shutdown = AsyncMock(side_effect=RuntimeError("shutdown failed"))
        with self.assertRaisesRegex(RuntimeError, "shutdown failed"):
            async with _application_lifespan(application):
                pass
        self.assertEqual(events[-1], "post_shutdown")

    async def test_failed_webhook_registration_closes_listener_before_application(self):
        events = []
        application = fake_application(events)
        application.bot.set_webhook = AsyncMock(side_effect=RuntimeError("registration failed"))
        runner = SimpleNamespace(
            setup=AsyncMock(), cleanup=AsyncMock(side_effect=lambda: events.append("cleanup_http"))
        )
        site = SimpleNamespace(start=AsyncMock())
        with patch.object(server, "create_webhook_app"), patch.object(web, "AppRunner", return_value=runner), patch.object(web, "TCPSite", return_value=site):
            with self.assertRaisesRegex(RuntimeError, "registration failed"):
                await server.run_webhook(application, WebhookSettings(url="https://bot.example.test"))
        self.assertLess(events.index("cleanup_http"), events.index("stop"))
        self.assertEqual(events[-2:], ["shutdown", "post_shutdown"])

    async def test_cancellation_closes_listener_before_application(self):
        events = []
        application = fake_application(events)
        registered = asyncio.Event()
        application.bot.set_webhook = AsyncMock(side_effect=lambda **kwargs: registered.set())
        runner = SimpleNamespace(
            setup=AsyncMock(), cleanup=AsyncMock(side_effect=lambda: events.append("cleanup_http"))
        )
        site = SimpleNamespace(start=AsyncMock())
        with patch.object(server, "create_webhook_app"), patch.object(web, "AppRunner", return_value=runner), patch.object(web, "TCPSite", return_value=site):
            task = asyncio.create_task(server.run_webhook(application, WebhookSettings(url="https://bot.example.test")))
            try:
                await asyncio.wait_for(registered.wait(), timeout=2)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertLess(events.index("cleanup_http"), events.index("stop"))
        self.assertEqual(events[-2:], ["shutdown", "post_shutdown"])
