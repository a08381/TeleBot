import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from plugins.e5 import e5_renewal
from plugins.wm import wm_api
from plugins.yiff import http_pool


class PluginResourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_database_call_waits_for_worker_to_finish(self):
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def operation():
            entered.set()
            release.wait(timeout=5)
            finished.set()

        task = asyncio.create_task(e5_renewal.store_call(operation))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
        finally:
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(finished.is_set())

    async def test_failed_renewal_waits_for_other_accounts_before_closing_http(self):
        with tempfile.TemporaryDirectory() as directory:
            service = e5_renewal.E5RenewalService({
                "database_path": str(Path(directory) / "e5.sqlite3"),
                "request_concurrency": 2,
                "request_timeout": 1,
            })
            entered = asyncio.Event()
            release = asyncio.Event()
            finished = asyncio.Event()
            service.store.list_enabled = Mock(return_value=["first", "second"])

            async def renew(client, account):
                if account == "first":
                    raise ValueError("database failed")
                entered.set()
                await release.wait()
                finished.set()
                return object()

            service._renew_account = renew
            http_context = AsyncMock()
            with patch.object(e5_renewal, "_httpx_module", return_value=SimpleNamespace(AsyncClient=Mock(return_value=http_context))):
                task = asyncio.create_task(service.run_all())
                try:
                    await asyncio.wait_for(entered.wait(), timeout=2)
                    self.assertFalse(task.done())
                    http_context.__aexit__.assert_not_awaited()
                finally:
                    release.set()
                    with self.assertRaisesRegex(ValueError, "database failed"):
                        await task
            self.assertTrue(finished.is_set())
            http_context.__aexit__.assert_awaited_once()

    async def test_cancelled_websocket_authentication_closes_unregistered_socket(self):
        with tempfile.TemporaryDirectory() as directory:
            client = wm_api.WMClient(session_path=Path(directory) / "sessions.json")
            socket = SimpleNamespace(send=AsyncMock(), close=AsyncMock())
            entered = asyncio.Event()

            async def wait_for_reply(*args):
                entered.set()
                await asyncio.Event().wait()

            with patch.object(wm_api.websockets, "connect", AsyncMock(return_value=socket)), patch.object(client, "_wait_for_socket_reply", wait_for_reply):
                task = asyncio.create_task(client._open_status_socket(wm_api.WMSession(tg_user_id=101, token="test")))
                await asyncio.wait_for(entered.wait(), timeout=2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            socket.close.assert_awaited_once()
            self.assertEqual(client._status_sockets, {})

    async def test_failed_wm_shutdown_retains_client_for_retry(self):
        client = SimpleNamespace(aclose=AsyncMock(side_effect=OSError("close failed")))
        with patch.object(wm_api, "_client", client), patch.object(wm_api, "_desired", client):
            with self.assertRaises(OSError):
                await wm_api.wm_stop()
            self.assertIs(wm_api.current(), client)

    async def test_failed_site_shutdown_retains_client_for_retry(self):
        client = SimpleNamespace(aclose=AsyncMock(side_effect=OSError("close failed")))
        with patch.object(http_pool, "_client", client):
            with self.assertRaises(OSError):
                await http_pool.http_stop()
            self.assertIs(http_pool._client, client)
