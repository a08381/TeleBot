import asyncio
import time
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from plugins.wm.listings import CB_LISTING, VIEW_TTL, Listings, market_sell_prices
from plugins.wm.wm_api import WMError, WMSession
from plugins.wm.wm_items import ItemIndex


def make_order(order_id="order-a", item_id="item-a", **changes):
    return {
        "id": order_id, "item_id": item_id, "type": "sell", "platinum": 30,
        "quantity": 3, "rank": 0, "visible": True, "user_status": "online",
        **changes,
    }


def context(user_id=101, callback=False):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id), is_callback=callback,
        message=SimpleNamespace(id=123, chat_id=101),
        bot=SimpleNamespace(edit_message_text=AsyncMock()),
        reply_text=AsyncMock(), reply_photo=AsyncMock(), edit_photo=AsyncMock(),
        answer_query=AsyncMock(),
    )


class ListingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.index = ItemIndex()
        self.index.load([
            {"id": "item-a", "slug": "alpha", "name": "Alpha", "thumb": "alpha.png", "maxRank": 5},
            {"id": "item-b", "slug": "beta", "name": "Beta", "thumb": "beta.png"},
            {"id": "item-c", "slug": "gamma", "name": "Gamma", "thumb": "gamma.png"},
        ])
        self.orders = [make_order(), make_order("order-b", "item-b"), make_order("order-c", "item-c")]
        self.market = [make_order("competitor-a", platinum=10),
                       make_order("competitor-b", platinum=10),
                       make_order("competitor-c", platinum=20)]
        self.session = WMSession(tg_user_id=101, token="fixture", wm_user_id="account-a", platform="pc")
        self.client = SimpleNamespace(
            platform="pc", crossplay=False, session_for=lambda user_id: self.session,
            get_my_orders=AsyncMock(side_effect=lambda session: deepcopy(self.orders)),
            get_orders=AsyncMock(side_effect=lambda *a, **kw: deepcopy(self.market)),
            get_last_sale_price=AsyncMock(return_value={"platinum": 25, "datetime": "2026-01-01T00:00:00Z"}),
            update_order_price=AsyncMock(side_effect=self.update_price),
            sell_order=AsyncMock(side_effect=self.sell),
        )
        self.patcher = patch("plugins.wm.listings.wm", AsyncMock(return_value=self.client))
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.ui = Listings({"my_listings": {"page_size": 2}}, AsyncMock(return_value=self.index))
        self.ctx = context()

    async def update_price(self, order_id, price, session):
        order = next(order for order in self.orders if order["id"] == order_id)
        order["platinum"] = price
        return deepcopy(order)

    async def sell(self, order_id, quantity, session, *, platinum=None):
        order = next(order for order in self.orders if order["id"] == order_id)
        order["quantity"] -= quantity
        self.orders = [order for order in self.orders if order["quantity"] > 0]
        return {"quantity": quantity, "platinum": platinum, "confirmed": True}

    async def open_view(self):
        await self.ui.show(self.ctx)
        return next(reversed(self.ui.views))

    async def click(self, key, action, user_id=101):
        ctx = context(user_id, callback=True)
        await self.ui.handle(ctx, f"{key}:{action}")
        return ctx

    async def test_photo_and_directory_buttons_have_all_actions(self):
        key = await self.open_view()
        kwargs = self.ctx.reply_photo.call_args.kwargs
        self.assertLessEqual(len(kwargs["caption"]), 1024)
        self.assertEqual(self.ctx.reply_photo.call_args.args[0], "https://warframe.market/static/assets/alpha.png")
        buttons = [button for row in kwargs["reply_markup"].rows for button in row]
        data = [button.callback_data for button in buttons if button.callback_data]
        for action in ("keep", "low", "second", "last", "sell1", "sellall", "bump"):
            self.assertIn(f"{CB_LISTING}{key}:{action}", data)
        self.assertIn(f"{CB_LISTING}{key}:select:1", data)
        self.assertTrue(all(len(value.encode("utf-8")) <= 64 for value in data))
        self.assertEqual(self.ui.views[key].targets, {"keep": 30, "low": 10, "second": 20, "last": 25})
        self.client.get_last_sale_price.assert_awaited_with("item-a", self.session, rank=0)

    async def test_select_changes_image_in_place(self):
        key = await self.open_view()
        ctx = await self.click(key, "select:1")
        ctx.edit_photo.assert_awaited_once()
        self.assertIn("beta.png", ctx.edit_photo.call_args.args[0])
        ctx.reply_photo.assert_not_awaited()
        self.assertNotIn(key, self.ui.views)

    async def test_refresh_button_bypasses_market_cache(self):
        key = await self.open_view()
        await self.click(key, "refresh")
        self.assertTrue(self.client.get_orders.call_args.kwargs["force"])

    async def test_next_page_uses_stable_sort_when_api_order_changes(self):
        key = await self.open_view()
        self.orders.reverse()
        ctx = await self.click(key, "page:1")
        self.assertIn("gamma.png", ctx.edit_photo.call_args.args[0])
        self.assertIn("2/2 页", ctx.edit_photo.call_args.kwargs["caption"])

    async def test_invalid_page_and_index_are_rejected(self):
        key = await self.open_view()
        for action in ("select:99", "page:-1", "page:no", "select"):
            ctx = await self.click(key, action)
            self.assertTrue(ctx.answer_query.call_args.kwargs["show_alert"])
        self.assertIn(key, self.ui.views)

    async def test_keep_price_still_patches_and_cannot_be_replayed(self):
        key = await self.open_view()
        await self.click(key, "keep")
        await self.click(key, "keep")
        self.client.update_order_price.assert_awaited_once_with("order-a", 30, self.session)

    async def test_all_four_price_strategies(self):
        for action, target in (("keep", 30), ("low", 10), ("second", 20), ("last", 25)):
            self.orders[0]["platinum"] = 30
            key = await self.open_view()
            await self.click(key, action)
            self.assertEqual(self.orders[0]["platinum"], target)
        self.assertTrue(any(call.kwargs.get("force") for call in self.client.get_orders.await_args_list))

    async def test_missing_competitors_and_history_do_not_disable_keep_or_sale(self):
        self.market = []
        self.client.get_last_sale_price.return_value = None
        key = await self.open_view()
        ctx = await self.click(key, "last")
        self.assertTrue(ctx.answer_query.call_args.kwargs["show_alert"])
        self.assertIn(key, self.ui.views)
        await self.click(key, "keep")
        self.client.update_order_price.assert_awaited_once()

    async def test_reference_failures_still_show_actionable_card(self):
        self.client.get_orders.side_effect = WMError("市场暂不可用")
        self.client.get_last_sale_price.side_effect = WMError("成交暂不可用")
        key = await self.open_view()
        self.assertIsNone(self.ui.views[key].targets["low"])
        self.ctx.reply_photo.assert_awaited_once()
        await self.click(key, "sell1")
        self.client.sell_order.assert_awaited_once()

    async def test_fresh_market_target_change_skips_write(self):
        key = await self.open_view()
        self.market[0]["platinum"] = 12
        self.market[1]["platinum"] = 12
        ctx = await self.click(key, "low")
        self.client.update_order_price.assert_not_awaited()
        self.assertIn("参考价已变化", ctx.edit_photo.call_args.kwargs["caption"])

    async def test_fractional_last_sale_price_is_rounded_and_explained(self):
        self.client.get_last_sale_price.return_value = {"platinum": 25.5, "source": "local"}
        key = await self.open_view()
        self.assertEqual(self.ui.views[key].targets["last"], 26)
        self.assertIn("成交回执 25.5p", self.ctx.reply_photo.call_args.kwargs["caption"])
        self.assertIn("四舍五入为 26p", self.ctx.reply_photo.call_args.kwargs["caption"])
        await self.click(key, "last")
        self.client.update_order_price.assert_awaited_once_with("order-a", 26, self.session)

    async def test_fresh_sale_price_change_skips_write(self):
        key = await self.open_view()
        self.client.get_last_sale_price.return_value = {"platinum": 26}
        await self.click(key, "last")
        self.client.update_order_price.assert_not_awaited()

    async def test_current_order_price_quantity_rank_visibility_change_skips_write(self):
        for field, value in (("quantity", 2), ("platinum", 31), ("rank", 1), ("visible", False)):
            self.orders[0] = make_order()
            key = await self.open_view()
            self.orders[0][field] = value
            ctx = await self.click(key, "sellall")
            self.assertIn("挂单价格、数量或状态已变化", ctx.edit_photo.call_args.kwargs["caption"])
        self.client.sell_order.assert_not_awaited()

    async def test_sell_one_reduces_stock_and_updates_card(self):
        key = await self.open_view()
        ctx = await self.click(key, "sell1")
        self.client.sell_order.assert_awaited_once_with("order-a", 1, self.session, platinum=30)
        self.assertEqual(self.orders[0]["quantity"], 2)
        self.assertIn("已售出 1 件", ctx.edit_photo.call_args.kwargs["caption"])
        await self.click(key, "sell1")
        self.assertEqual(self.client.sell_order.await_count, 1)

    async def test_sell_all_only_affects_selected_order(self):
        key = await self.open_view()
        ctx = await self.click(key, "sellall")
        self.client.sell_order.assert_awaited_once_with("order-a", 3, self.session, platinum=30)
        self.assertEqual([order["id"] for order in self.orders], ["order-b", "order-c"])
        self.assertIn("beta.png", ctx.edit_photo.call_args.args[0])

    async def test_selling_last_item_renders_empty_state(self):
        self.orders = [make_order(quantity=1)]
        key = await self.open_view()
        ctx = await self.click(key, "sellall")
        self.assertEqual(self.orders, [])
        self.assertIn("当前没有卖单", ctx.bot.edit_message_text.call_args.kwargs["text"])
        self.assertIn("已售出 1 件", ctx.bot.edit_message_text.call_args.kwargs["text"])

    async def test_account_id_resolved_during_render_is_bound_to_buttons(self):
        self.session.wm_user_id = ""

        async def last_sale(*args, **kwargs):
            self.session.wm_user_id = "resolved-account"
            return None

        self.client.get_last_sale_price.side_effect = last_sale
        key = await self.open_view()
        self.assertEqual(self.ui.views[key].account, "resolved-account")
        await self.click(key, "keep")
        self.client.update_order_price.assert_awaited_once()

    async def test_ownership_rebinding_and_expiration_block_writes(self):
        key = await self.open_view()
        await self.click(key, "sellall", user_id=202)
        self.assertIn(key, self.ui.views)
        self.session.wm_user_id = "another-account"
        await self.click(key, "sellall")
        self.session.wm_user_id = "account-a"
        self.ui.views[key].created = time.monotonic() - VIEW_TTL - 1
        await self.click(key, "sellall")
        self.client.sell_order.assert_not_awaited()

    async def test_two_views_do_not_execute_writes_concurrently(self):
        first = await self.open_view()
        second = await self.open_view()
        started, release = asyncio.Event(), asyncio.Event()

        async def waiting_sale(*args, **kwargs):
            started.set()
            await release.wait()
            return await self.sell(*args, **kwargs)

        self.client.sell_order.side_effect = waiting_sale
        task = asyncio.create_task(self.click(first, "sell1"))
        await started.wait()
        ctx = await self.click(second, "sell1")
        self.assertIn("仍在处理中", ctx.answer_query.call_args.args[0])
        release.set()
        await task
        await self.click(second, "sell1")
        self.assertEqual(self.client.sell_order.await_count, 1)

    async def test_per_trade_greater_than_one_blocks_single_sale(self):
        self.orders[0]["per_trade"] = 3
        key = await self.open_view()
        ctx = await self.click(key, "sell1")
        self.client.sell_order.assert_not_awaited()
        self.assertIn("每次交易 3 件", ctx.edit_photo.call_args.kwargs["caption"])
        await self.click(next(reversed(self.ui.views)), "sellall")
        self.client.sell_order.assert_awaited_once_with("order-a", 3, self.session, platinum=30)

    async def test_sale_feedback_uses_actual_receipt_instead_of_requested_values(self):
        key = await self.open_view()
        self.client.sell_order.side_effect = None
        self.client.sell_order.return_value = {"confirmed": True, "quantity": 2, "platinum": 31}
        ctx = await self.click(key, "sellall")
        self.assertIn("已售出 2 件，实际成交价 31p", ctx.edit_photo.call_args.kwargs["caption"])

    async def test_sale_without_receipt_does_not_claim_success_or_retry(self):
        key = await self.open_view()
        self.client.sell_order.side_effect = None
        self.client.sell_order.return_value = {"confirmed": False}
        ctx = await self.click(key, "sellall")
        self.assertIn("未返回成交明细", ctx.edit_photo.call_args.kwargs["caption"])
        await self.click(key, "sellall")
        self.client.sell_order.assert_awaited_once()

    async def test_network_failure_is_not_retried(self):
        key = await self.open_view()
        self.client.sell_order.side_effect = WMError("请求超时", kind="network")
        ctx = await self.click(key, "sell1")
        await self.click(key, "sell1")
        self.assertEqual(self.client.sell_order.await_count, 1)
        self.assertIn("操作未获确认", ctx.edit_photo.call_args.kwargs["caption"])
        self.assertEqual(self.ui.busy, set())

    async def test_success_notice_survives_refresh_error(self):
        key = await self.open_view()
        self.client.get_my_orders.side_effect = [deepcopy(self.orders), WMError("刷新超时")]
        ctx = await self.click(key, "sell1")
        text = ctx.reply_text.call_args.args[0]
        self.assertIn("已售出 1 件", text)
        self.assertIn("刷新卖单失败", text)

    async def test_photo_failure_falls_back_to_text_with_buttons(self):
        self.ctx.reply_photo.side_effect = RuntimeError("图片地址失效")
        await self.open_view()
        self.assertIn("reply_markup", self.ctx.reply_text.call_args.kwargs)

    async def test_bulk_keep_includes_hidden_sells_not_buys_and_preserves_quantity(self):
        self.orders[1]["visible"] = False
        self.orders[2]["type"] = "buy"
        before = deepcopy(self.orders)
        await self.ui.bump(self.ctx)
        self.assertEqual(self.client.update_order_price.await_count, 2)
        self.assertEqual(self.orders, before)
        self.client.get_orders.assert_not_awaited()
        self.assertIn("成功 2 条", self.ctx.reply_text.call_args.args[0])

    async def test_bulk_failure_reports_partial_result_and_stops_on_rate_limit(self):
        self.client.update_order_price.side_effect = [make_order(), WMError("限流", kind="rate")]
        await self.ui.bump(self.ctx)
        text = self.ctx.reply_text.call_args.args[0]
        self.assertIn("成功 1 条", text)
        self.assertIn("未确认 1 条", text)
        self.assertIn("未尝试 1 条", text)
        self.assertEqual(self.ui.busy, set())

    async def test_invalid_page_size_and_out_of_range_page_are_clamped(self):
        self.ui.settings["my_listings"]["page_size"] = "bad"
        await self.ui.show(self.ctx, 99)
        view = next(iter(self.ui.views.values()))
        self.assertEqual(view.page, 0)
        self.assertEqual(view.page_size, 8)


class MarketAndImageTests(unittest.TestCase):
    def test_filters_exclude_own_hidden_offline_wrong_rank_and_empty_orders(self):
        orders = [
            make_order("own", platinum=1), make_order("hidden", visible=False, platinum=2),
            make_order("offline", user_status="offline", platinum=3), make_order("rank", rank=1, platinum=4),
            make_order("empty", quantity=0, platinum=5), make_order("buy", type="buy", platinum=6),
            make_order("ok", platinum=10), make_order("bad", platinum="bad"),
        ]
        self.assertEqual(market_sell_prices(orders, rank=0, own_order_ids={"own"}, only_online=True), [10])

    def test_icon_and_non_english_thumbnail_fallback(self):
        index = ItemIndex()
        index.load([
            {"id": "one", "slug": "one", "icon": "one.png"},
            {"id": "two", "slug": "two", "i18n": {"zh-hans": {"thumb": "two.png"}}},
        ])
        self.assertEqual(index.get_by_id("one").thumb, "one.png")
        self.assertEqual(index.get_by_id("two").thumb, "two.png")


if __name__ == "__main__":
    unittest.main()
