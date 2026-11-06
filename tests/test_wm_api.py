import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from plugins.wm.wm_api import WMClient, WMError, WMSession, normalize_order


def response(data=None, *, status=200, envelope=True, invalid_json=False):
    body = {"apiVersion": "0.25.0", "data": data, "error": None} if envelope else data
    read_json = Mock(side_effect=ValueError("非 JSON 响应")) if invalid_json else Mock(return_value=body)
    return SimpleNamespace(status_code=status, headers={}, json=read_json)


def order(**changes):
    data = {
        "id": "order-1", "itemId": "item-1", "type": "sell", "platinum": 20,
        "quantity": 4, "rank": 0, "visible": True,
    }
    return {**data, **changes}


def transaction(**changes):
    data = {
        "id": "tx-1", "originId": "order-1", "type": "sell", "platinum": 20,
        "quantity": 1, "createdAt": "2026-11-06T12:00:00Z",
        "updatedAt": "2026-11-06T12:00:00Z", "item": {"id": "item-1", "rank": 0},
    }
    return {**data, **changes}


class NormalizeOrderTests(unittest.TestCase):
    def test_bulk_trade_units_are_preserved(self):
        normalized = normalize_order(order(perTrade=3, quantity=6))
        self.assertEqual(normalized["per_trade"], 3)
        self.assertEqual(normalized["quantity"], 6)
        self.assertEqual(normalize_order(order())["per_trade"], 1)

    def test_zero_and_invalid_quantities_are_not_invented_as_one(self):
        for quantity in (0, "bad", None, True, 1.5):
            with self.subTest(quantity=quantity):
                self.assertEqual(normalize_order(order(quantity=quantity))["quantity"], 0)
        self.assertEqual(normalize_order({})["quantity"], 0)
        self.assertEqual(normalize_order(order(perTrade="bad"))["per_trade"], 0)
        self.assertEqual(normalize_order(order(quantity="4"))["quantity"], 4)


class WMClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.session_path = Path(self.directory.name) / "wm_sessions.json"
        self.sales_path = self.session_path.with_name("wm_sales.json")
        self.client = WMClient(session_path=self.session_path)
        self.client._limiter.acquire = AsyncMock()
        self.http = SimpleNamespace(request=AsyncMock(side_effect=AssertionError("测试不应访问网络")))
        self.client._client = self.http
        self.session = WMSession(tg_user_id=101, token="test-token", wm_user_id="account-1")

    def arrange_sale(self, *, listing=None, receipt=None):
        self.http.request.side_effect = [
            response([order() if listing is None else listing]),
            response(transaction() if receipt is None else receipt),
        ]

    async def test_one_sale_reads_owned_orders_then_posts_exact_quantity_once(self):
        self.arrange_sale()
        self.client._orders_cache["cached"] = (0, [])
        result = await self.client.sell_order("order-1", 1, self.session, platinum=20)

        self.assertTrue(result["confirmed"])
        self.assertTrue(result["history_recorded"])
        self.assertEqual(result["quantity"], 1)
        self.assertEqual(result["platinum"], 20)
        self.assertEqual(result["transaction_id"], "tx-1")
        self.assertEqual(result["requested_quantity"], 1)
        self.assertEqual(self.client._orders_cache, {})
        calls = self.http.request.await_args_list
        self.assertEqual([call.args[0] for call in calls], ["GET", "POST"])
        self.assertTrue(calls[0].args[1].endswith("/v2/orders/my"))
        self.assertTrue(calls[1].args[1].endswith("/v2/order/order-1/close"))
        self.assertEqual(calls[1].kwargs["json"], {"quantity": 1})
        self.assertEqual(calls[1].kwargs["headers"]["Authorization"], "Bearer test-token")
        self.assertEqual(calls[1].kwargs["headers"]["Content-Type"], "application/json")

    async def test_all_uses_fresh_quantity_not_an_old_card_snapshot(self):
        self.arrange_sale(listing=order(quantity=3), receipt=transaction(quantity=3))
        result = await self.client.sell_order("order-1", "all", self.session)
        self.assertEqual(result["quantity"], 3)
        self.assertEqual(result["requested_quantity"], 3)
        self.assertEqual(self.http.request.await_args.kwargs["json"], {"quantity": 3})

    async def test_bulk_order_rejects_one_but_accepts_all_without_patching(self):
        self.http.request.side_effect = [response([order(quantity=6, perTrade=3)])]
        with self.assertRaisesRegex(WMError, "每次交易 3 件"):
            await self.client.sell_order("order-1", 1, self.session)
        self.assertEqual(self.http.request.await_count, 1)
        self.http.request.reset_mock()
        self.arrange_sale(listing=order(quantity=6, perTrade=3), receipt=transaction(quantity=6))
        result = await self.client.sell_order("order-1", "all", self.session)
        self.assertEqual(result["quantity"], 6)
        self.assertEqual([c.args[0] for c in self.http.request.await_args_list], ["GET", "POST"])

    async def test_sale_reports_real_clamped_quantity_and_changed_price(self):
        self.arrange_sale(receipt=transaction(quantity=2, platinum=22.5))
        result = await self.client.sell_order("order-1", "all", self.session, platinum=20)
        self.assertEqual(result["requested_quantity"], 4)
        self.assertEqual(result["quantity"], 2)
        self.assertEqual(result["platinum"], 22.5)
        self.assertEqual((await self.client.get_last_sale_price("item-1", self.session))["platinum"], 22.5)

    async def test_changed_price_stops_before_writing(self):
        self.http.request.side_effect = [response([order(platinum=21)])]
        with self.assertRaises(WMError) as caught:
            await self.client.sell_order("order-1", 1, self.session, platinum=20)
        self.assertEqual(caught.exception.code, "order_changed")
        self.assertEqual(self.http.request.await_count, 1)

    async def test_invalid_input_never_makes_a_request(self):
        for quantity in (0, -1, 10000, True, False, 1.5, "1", "one", "ALL", None):
            with self.subTest(quantity=quantity), self.assertRaises(WMError):
                await self.client.sell_order("order-1", quantity, self.session)
        for price in (0, -1, 900001, True, 1.5, "20"):
            with self.subTest(price=price), self.assertRaises(WMError):
                await self.client.sell_order("order-1", 1, self.session, platinum=price)
        with self.assertRaises(WMError):
            await self.client.sell_order(" ", 1, self.session)
        self.http.request.assert_not_awaited()

    async def test_missing_login_never_makes_a_request(self):
        for session in (None, WMSession(tg_user_id=101, wm_user_id="account-1")):
            with self.subTest(session=session), self.assertRaises(WMError) as caught:
                await self.client.sell_order("order-1", 1, session)
            self.assertEqual(caught.exception.kind, "auth")
        self.http.request.assert_not_awaited()

    async def test_other_account_missing_buy_and_short_orders_do_not_write(self):
        for listings in ([], [order(id="other-order")], [order(type="buy")], [order(quantity=0)]):
            self.http.request.reset_mock()
            self.http.request.side_effect = [response(listings)]
            with self.subTest(listings=listings), self.assertRaises(WMError):
                await self.client.sell_order("order-1", 1, self.session)
            self.assertEqual(self.http.request.await_count, 1)
        self.http.request.reset_mock()
        self.http.request.side_effect = [response([order(quantity=1)])]
        with self.assertRaises(WMError):
            await self.client.sell_order("order-1", 2, self.session)
        self.assertEqual(self.http.request.await_count, 1)

    async def test_order_id_is_path_encoded(self):
        self.arrange_sale(listing=order(id="order/ ?"), receipt=transaction(originId="order/ ?"))
        await self.client.sell_order("order/ ?", 1, self.session)
        self.assertTrue(self.http.request.await_args.args[1].endswith("/order/order%2F%20%3F/close"))

    async def test_missing_account_id_is_loaded_from_me_before_sale(self):
        self.session.wm_user_id = ""
        self.http.request.side_effect = [
            response({"id": "account-2", "ingameName": "Test"}),
            response([order()]), response(transaction(user={"id": "account-2"})),
        ]
        await self.client.sell_order("order-1", 1, self.session)
        saved = await self.client.get_last_sale_price("item-1", self.session)
        self.assertEqual(saved["wm_user_id"], "account-2")
        self.assertTrue(self.http.request.await_args_list[0].args[1].endswith("/v2/me"))

    async def test_empty_success_does_not_invent_a_receipt_or_retry(self):
        for close_response in (
            response(status=204, invalid_json=True),
            response(None),
            response(None, envelope=False),
        ):
            self.http.request.reset_mock()
            self.http.request.side_effect = [response([order()]), close_response]
            self.client._orders_cache["cached"] = (0, [])
            with self.subTest(status=close_response.status_code):
                result = await self.client.sell_order("order-1", 1, self.session)
                self.assertFalse(result["confirmed"])
                self.assertFalse(result["history_recorded"])
                for key in ("quantity", "platinum", "datetime", "transaction_id"):
                    self.assertIsNone(result[key])
                self.assertEqual(self.http.request.await_count, 2)
                self.assertEqual(self.client._orders_cache, {})
                self.assertIsNone(await self.client.get_last_sale_price("item-1", self.session))
                self.assertFalse(self.sales_path.exists())

    async def test_default_call_still_requires_payload(self):
        self.http.request.side_effect = [response(None)]
        with self.assertRaises(WMError) as caught:
            await self.client.call("GET", "https://api.example.test/orders", session=self.session)
        self.assertEqual(caught.exception.code, "response_format")

    async def test_malformed_success_is_unknown_not_an_empty_receipt(self):
        for close_response in (response(invalid_json=True), response({}, envelope=False), response([])):
            self.http.request.reset_mock()
            self.http.request.side_effect = [response([order()]), close_response]
            with self.subTest(close_response=close_response), self.assertRaises(WMError) as caught:
                await self.client.sell_order("order-1", 1, self.session)
            self.assertEqual(caught.exception.code, "sale_unconfirmed")
            self.assertEqual(self.http.request.await_count, 2)
        self.assertFalse(self.sales_path.exists())

    async def test_http_and_envelope_errors_are_never_sold(self):
        cases = ((401, "auth"), (403, "auth"), (404, "api"), (429, "rate"), (500, "api"), (302, "api"))
        for status, kind in cases:
            self.http.request.reset_mock()
            self.http.request.side_effect = [response([order()]), response(status=status)]
            with self.subTest(status=status), self.assertRaises(WMError) as caught:
                await self.client.sell_order("order-1", 1, self.session)
            self.assertEqual(caught.exception.kind, kind)
            self.assertEqual(self.http.request.await_count, 2)
        for status in (200, 204):
            self.http.request.side_effect = [
                response([order()]),
                response({"data": None, "error": {"request": ["app.errors.denied"]}}, status=status, envelope=False),
            ]
            with self.assertRaises(WMError):
                await self.client.sell_order("order-1", 1, self.session)
        self.assertFalse(self.sales_path.exists())

    async def test_transport_failure_invalidates_cache_and_is_not_retried(self):
        self.http.request.side_effect = [response([order()]), OSError("连接中断")]
        self.client._orders_cache["cached"] = (0, [])
        with self.assertRaises(WMError) as caught:
            await self.client.sell_order("order-1", 1, self.session)
        self.assertEqual(caught.exception.code, "sale_unconfirmed")
        self.assertEqual(self.client._orders_cache, {})
        self.assertEqual(self.http.request.await_count, 2)
        self.assertFalse(self.sales_path.exists())

    async def test_bad_receipt_identity_amount_or_time_is_not_recorded(self):
        cases = (
            {"id": ""}, {"originId": "other"}, {"type": "buy"}, {"quantity": 0},
            {"quantity": 2}, {"quantity": True}, {"platinum": True}, {"platinum": 0},
            {"platinum": float("nan")}, {"platinum": float("inf")},
            {"createdAt": "2026-11-06T12:00:00"}, {"createdAt": "invalid"},
            {"user": {"id": "other-account"}}, {"item": {"id": "other-item", "rank": 0}},
        )
        for changes in cases:
            self.arrange_sale(receipt=transaction(**changes))
            with self.subTest(changes=changes), self.assertRaises(WMError) as caught:
                await self.client.sell_order("order-1", 1, self.session)
            self.assertEqual(caught.exception.code, "sale_unconfirmed")
        self.assertFalse(self.sales_path.exists())

    async def test_compact_receipt_uses_same_order_context_and_marks_its_source(self):
        compact = transaction()
        compact.pop("item")
        self.arrange_sale(listing=order(rank=5), receipt=compact)
        result = await self.client.sell_order("order-1", 1, self.session)
        self.assertTrue(result["confirmed"])
        self.assertTrue(result["history_recorded"])
        self.assertEqual(result["identity_source"], "order_snapshot")
        self.assertIsNone(await self.client.get_last_sale_price("item-1", self.session, rank=0))
        saved = await self.client.get_last_sale_price("item-1", self.session, rank=5)
        self.assertEqual(saved["identity_source"], "order_snapshot")
        self.assertEqual(saved["source"], "local")
        self.assertEqual(saved["platinum"], 20)

    async def test_compact_item_without_rank_preserves_order_rank_and_marks_mixed_source(self):
        self.arrange_sale(listing=order(rank=5), receipt=transaction(item={"id": "item-1"}))
        result = await self.client.sell_order("order-1", 1, self.session)
        self.assertEqual(result["rank"], 5)
        self.assertEqual(result["identity_source"], "mixed")
        self.assertIsNotNone(await self.client.get_last_sale_price("item-1", self.session, rank=5))

    async def test_receipt_rank_is_used_instead_of_pre_read_rank(self):
        self.arrange_sale(receipt=transaction(item={"id": "item-1", "rank": 5}))
        result = await self.client.sell_order("order-1", 1, self.session)
        self.assertEqual(result["rank"], 5)
        self.assertIsNone(await self.client.get_last_sale_price("item-1", self.session, rank=0))
        self.assertIsNotNone(await self.client.get_last_sale_price("item-1", self.session, rank=5))

    async def test_local_history_is_scoped_to_account_item_and_rank_and_returns_a_copy(self):
        self.arrange_sale()
        await self.client.sell_order("order-1", 1, self.session)
        self.http.request.reset_mock()
        other_account = WMSession(tg_user_id=101, token="other-token", wm_user_id="account-2")
        same_account = WMSession(tg_user_id=202, token="other-token", wm_user_id="account-1")
        self.assertIsNone(await self.client.get_last_sale_price("item-1", other_account))
        self.assertIsNone(await self.client.get_last_sale_price("other-item", self.session))
        self.assertIsNone(await self.client.get_last_sale_price("item-1", self.session, rank=1))
        saved = await self.client.get_last_sale_price("item-1", same_account, rank=None)
        self.assertEqual(saved["source"], "local")
        self.assertEqual(saved["datetime"], "2026-11-06T12:00:00Z")
        saved["platinum"] = 999
        self.assertEqual((await self.client.get_last_sale_price("item-1", self.session))["platinum"], 20)
        self.http.request.assert_not_awaited()

    async def test_latest_uses_creation_instant_not_update_time_or_string_order(self):
        self.arrange_sale(receipt=transaction(id="new", createdAt="2026-11-06T11:30:00Z"))
        await self.client.sell_order("order-1", 1, self.session)
        self.arrange_sale(receipt=transaction(
            id="old", platinum=10, createdAt="2026-11-06T13:00:00+02:00",
            updatedAt="2026-11-07T12:00:00Z",
        ))
        await self.client.sell_order("order-1", 1, self.session)
        result = await self.client.get_last_sale_price("item-1", self.session)
        self.assertEqual(result["transaction_id"], "new")
        stored = json.loads(self.sales_path.read_text(encoding="utf-8"))
        self.assertEqual(len(stored["sales"]), 1)
        self.assertNotIn("test-token", self.sales_path.read_text(encoding="utf-8"))

    async def test_history_survives_a_new_client(self):
        self.arrange_sale()
        await self.client.sell_order("order-1", 1, self.session)
        reloaded = WMClient(session_path=self.session_path)
        result = await reloaded.get_last_sale_price("item-1", self.session)
        self.assertEqual(result["platinum"], 20)
        self.assertEqual(result["source"], "local")

    async def test_disk_failure_keeps_actual_sale_success_and_memory_record(self):
        self.arrange_sale()
        with patch.object(Path, "replace", side_effect=OSError("磁盘写入失败")):
            with self.assertLogs("plugins.wm.wm_api", level="WARNING"):
                result = await self.client.sell_order("order-1", 1, self.session)
        self.assertTrue(result["confirmed"])
        self.assertIsNotNone(await self.client.get_last_sale_price("item-1", self.session))
        self.assertEqual(self.http.request.await_count, 2)

    async def test_corrupt_and_non_transaction_history_is_ignored(self):
        for text in ("not-json", "[]", '{"version":1,"sales":[null,{}, {"platinum": 999}]}'):
            self.sales_path.write_text(text, encoding="utf-8")
            with self.subTest(text=text):
                reloaded = WMClient(session_path=self.session_path)
                self.assertIsNone(await reloaded.get_last_sale_price("item-1", self.session))

    async def test_history_validation_and_authentication_do_not_fall_back_to_statistics(self):
        for item_id, rank in (("", 0), ("item-1", -1), ("item-1", True), ("item-1", "0")):
            with self.subTest(item_id=item_id, rank=rank), self.assertRaises(WMError):
                await self.client.get_last_sale_price(item_id, self.session, rank=rank)
        with self.assertRaises(WMError) as caught:
            await self.client.get_last_sale_price("item-1", WMSession())
        self.assertEqual(caught.exception.kind, "auth")
        self.assertIsNone(await self.client.get_last_sale_price("item-1", self.session))
        self.http.request.assert_not_awaited()

    async def test_concurrent_all_sales_refresh_under_the_same_lock(self):
        write_started = asyncio.Event()
        finish_write = asyncio.Event()
        requests = []
        sold = False

        async def request(method, url, **kwargs):
            nonlocal sold
            requests.append(method)
            if method == "GET":
                return response([] if sold else [order(quantity=1)])
            write_started.set()
            await finish_write.wait()
            sold = True
            return response(transaction())

        self.http.request.side_effect = request
        first = asyncio.create_task(self.client.sell_order("order-1", "all", self.session))
        await write_started.wait()
        second = asyncio.create_task(self.client.sell_order("order-1", "all", self.session))
        await asyncio.sleep(0)
        self.assertEqual(requests, ["GET", "POST"])
        finish_write.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        self.assertTrue(results[0]["confirmed"])
        self.assertIsInstance(results[1], WMError)
        self.assertEqual(requests, ["GET", "POST", "GET"])


if __name__ == "__main__":
    unittest.main()
