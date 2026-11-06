"""卖单图片浏览器：短期按钮快照、快捷改价和售出操作。

一条消息展示当前商品，分页目录负责切换；不使用媒体组，因为媒体组没有逐项键盘。
所有写操作重新读取自己的订单，按钮绑定 Telegram 用户及 WM 账号，且只能消费一次。
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from core import Context, Keyboard
from .wm_api import ITEM_PAGE, ONLINE_STATUSES, WMError, WMSession, thumb_url, wm
from .wm_items import Item, ItemIndex

logger = logging.getLogger(__name__)

CB_LISTING = "wm:list:"
VIEW_TTL = 600
MAX_VIEWS = 500
PRICE_LABELS = {"keep": "原价重提", "low": "最低价", "second": "次低价", "last": "最近售出价"}


def market_sell_prices(orders: list[dict], *, rank: int, own_order_ids: set[str],
                       only_online: bool) -> list[int]:
    prices = []
    for order in orders:
        if order.get("type") != "sell" or order.get("visible") is False:
            continue
        if str(order.get("id") or "") in own_order_ids:
            continue
        if only_online and order.get("user_status") not in ONLINE_STATUSES:
            continue
        try:
            if int(order.get("rank") or 0) != rank or int(order.get("quantity", 1)) <= 0:
                continue
            price = int(order.get("platinum"))
        except (TypeError, ValueError):
            continue
        if price > 0:
            prices.append(price)
    return sorted(prices)


def account_key(session: WMSession) -> str:
    """同一 WM 账号绑定多个 Telegram 用户时也共用操作锁；不把凭据放进按钮。"""
    return session.wm_user_id or session.token


def order_signature(order: dict) -> tuple:
    return tuple(order.get(name) for name in (
        "id", "item_id", "type", "rank", "platinum", "quantity", "visible", "per_trade",
    ))


def item_title(order: dict, index: ItemIndex) -> str:
    item = index.get_by_id(order.get("item_id", ""))
    title = item.display if item else str(order.get("item_id") or "未知物品")
    return title + (f" · r{order.get('rank', 0)}" if item and item.has_rank else "")


@dataclass
class ListingView:
    owner: int
    account: str
    orders: list[dict]
    selected: int
    page_size: int
    targets: dict[str, Optional[int]] = field(default_factory=dict)
    created: float = field(default_factory=time.monotonic)

    @property
    def order(self) -> dict:
        return self.orders[self.selected]

    @property
    def page(self) -> int:
        return self.selected // self.page_size

    @property
    def last_page(self) -> int:
        return (len(self.orders) - 1) // self.page_size


class Listings:
    def __init__(self, settings, ensure_items):
        self.settings = settings
        self.ensure_items = ensure_items
        self.views: dict[str, ListingView] = {}
        self.busy: set[str] = set()

    def _remember(self, view: ListingView) -> str:
        now = time.monotonic()
        for key, old in list(self.views.items()):
            if now - old.created > VIEW_TTL:
                self.views.pop(key, None)
        while len(self.views) >= MAX_VIEWS:
            self.views.pop(next(iter(self.views)))
        key = uuid.uuid4().hex[:12]
        self.views[key] = view
        return key

    def _page_size(self) -> int:
        try:
            size = int((self.settings.get("my_listings") or {}).get("page_size", 8))
        except (TypeError, ValueError):
            size = 8
        return max(1, min(20, size))

    async def _references(self, client, session, order: dict, item: Optional[Item],
                          own_ids: set[str], *, force: bool = False) -> tuple[dict, list[str]]:
        targets = {"keep": order["platinum"], "low": None, "second": None, "last": None}
        notes = []
        if item:
            try:
                orders = await client.get_orders(
                    item.slug, platform=session.platform or client.platform,
                    crossplay=client.crossplay, force=force,
                )
                # 次低价指第二个不同的价格档位，而不是第二个同价卖家。
                prices = sorted(set(market_sell_prices(
                    orders, rank=order.get("rank", 0), own_order_ids=own_ids,
                    only_online=bool((self.settings.get("price") or {}).get("only_online", True)),
                )))
                targets["low"] = prices[0] if prices else None
                targets["second"] = prices[1] if len(prices) > 1 else None
            except WMError:
                notes.append("市场参考价读取失败，可刷新重试")
        else:
            notes.append("物品名称/图片尚未匹配，可用 /wmrefresh 更新索引")
        try:
            sale = await client.get_last_sale_price(
                order["item_id"], session, rank=order.get("rank", 0),
            )
            if sale:
                actual = Decimal(str(sale["platinum"]))
                target = max(1, int(actual.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))
                targets["last"] = target
                if actual != target:
                    notes.append(f"成交回执 {actual}p；改价按整数四舍五入为 {target}p（至少 1p）")
                notes.append(f"本插件记录的成交时间：{sale.get('datetime') or '未记录'}")
        except WMError:
            notes.append("本账号成交记录读取失败，可刷新重试")
        return targets, notes

    def _keyboard(self, view: ListingView, key: str, index: ItemIndex) -> Keyboard:
        keyboard = Keyboard()
        for action in PRICE_LABELS:
            value = view.targets.get(action)
            label = f"{PRICE_LABELS[action]} {value}p" if value else f"{PRICE_LABELS[action]} · 暂无"
            keyboard.callback(label, f"{CB_LISTING}{key}:{action}")
            if action in ("low", "last"):
                keyboard.row()
        keyboard.callback("售出 1 件", f"{CB_LISTING}{key}:sell1")
        keyboard.callback(f"全部售出 ×{view.order['quantity']}", f"{CB_LISTING}{key}:sellall").row()
        keyboard.callback(
            "⬅️ 上一页" if view.page else "· 已到首页 ·",
            f"{CB_LISTING}{key}:page:{max(0, view.page - 1)}",
        ).row()
        start = view.page * view.page_size
        for position in range(start, min(start + view.page_size, len(view.orders))):
            order = view.orders[position]
            title = item_title(order, index)
            title = title if len(title) <= 32 else title[:31] + "…"
            keyboard.callback(
                f"{'▸ ' if position == view.selected else ''}{title}  {order['platinum']}p ×{order['quantity']}",
                f"{CB_LISTING}{key}:select:{position}",
            ).row()
        keyboard.callback(
            "下一页 ➡️" if view.page < view.last_page else "· 已到末页 ·",
            f"{CB_LISTING}{key}:page:{min(view.last_page, view.page + 1)}",
        ).row()
        keyboard.callback("🔄 刷新", f"{CB_LISTING}{key}:refresh")
        keyboard.callback("♻️ 全部卖单原价重提", f"{CB_LISTING}{key}:bump").row()
        item = index.get_by_id(view.order.get("item_id", ""))
        if item:
            keyboard.url("查看 WM 商品页", ITEM_PAGE.format(slug=item.slug))
        return keyboard

    async def _deliver(self, ctx: Context, text: str, keyboard: Keyboard, image: str, *, edit: bool):
        """优先编辑原消息；图片失效时回退文字，避免留下上一件商品的错误图片。"""
        if image:
            try:
                if edit:
                    await ctx.edit_photo(image, caption=text, reply_markup=keyboard, parse_mode=None)
                else:
                    await ctx.reply_photo(image, caption=text, reply_markup=keyboard, parse_mode=None)
                return
            except Exception as exc:
                logger.info("WM 图片卡片发送失败，退回文字：%s", exc)
        if edit and ctx.message:
            try:
                await ctx.bot.edit_message_text(
                    chat_id=ctx.message.chat_id, message_id=ctx.message.id,
                    text=text, reply_markup=keyboard.build(), parse_mode=None,
                )
                return
            except Exception:
                # 图片消息转回文字须新发一条；旧按钮快照已经失效。
                pass
        await ctx.reply_text(text, reply_markup=keyboard, parse_mode=None)

    async def show(self, ctx: Context, page: int = 0, *, selected_id: str = "",
                   edit: bool = False, notice: str = "", force: bool = False) -> None:
        user_id = int(ctx.user.id) if ctx.user else 0
        try:
            client = await wm()
            session = client.session_for(user_id)
            if not session.logged_in:
                raise WMError("请先 /wmlogin 绑定账号", kind="auth")
            orders = await client.get_my_orders(session)
            sells = [dict(order) for order in orders if order.get("type") == "sell" and order.get("quantity", 0) > 0]
            if not sells:
                text = "\n\n".join(filter(None, (notice, f"📦 {session.label} 当前没有卖单。")))
                await self._deliver(ctx, text, Keyboard().callback("返回账号面板", "wm:me"), "", edit=edit)
                return
            index = await self.ensure_items()
            # 改价会改变远端排序，浏览器按名称/等级/ID 排序，避免翻页跳项。
            sells.sort(key=lambda order: (item_title(order, index).casefold(), str(order.get("id"))))
            size = self._page_size()
            page = max(0, min(page, (len(sells) - 1) // size))
            selected = next((i for i, order in enumerate(sells) if order["id"] == selected_id), page * size)
            view = ListingView(user_id, account_key(session), sells, selected, size)
            order = view.order
            item = index.get_by_id(order.get("item_id", ""))
            view.targets, notes = await self._references(
                client, session, order, item, {order["id"] for order in orders}, force=force,
            )
            # 首次读取成交记录可能补齐 WM 用户 ID，用补齐后的账号标识绑定按钮。
            view.account = account_key(session)
            key = self._remember(view)
            keyboard = self._keyboard(view, key, index)
            only_online = bool((self.settings.get("price") or {}).get("only_online", True))
            lines = [
                f"📦 {session.label[:80]} 的卖单 · {view.page + 1}/{view.last_page + 1} 页 · 共 {len(sells)} 条",
                "", item_title(order, index)[:180],
                f"当前挂单  {order['platinum']}p  ×  {order['quantity']} 件",
                f"{'可见' if order.get('visible') is not False else '隐藏'} · {session.platform or client.platform}",
                "",
                f"最低 {view.targets['low'] or '—'}p  /  次低 {view.targets['second'] or '—'}p",
                f"最近售出参考 {view.targets['last'] or '—'}p（本插件记录，同商品同等级）",
                "参考价排除自己的卖单；次低为第二个不同价位。",
                "仅比较在线/游戏中卖家。" if only_online else "比较全部状态的卖家。",
                "原价重提保持当前价格；售出结果以 WM 回执为准。",
            ]
            if order.get("per_trade", 1) > 1:
                lines.append(f"每次交易 {order['per_trade']} 件；售出数量须为此数的整数倍。")
            lines.extend(notes)
            if notice:
                lines[0:0] = [notice[:230], ""]
            lines.extend(("", "点下方商品切换图片；售出按钮直接执行。"))
            await self._deliver(ctx, "\n".join(lines)[:1000], keyboard, thumb_url(item.thumb) if item else "", edit=edit)
        except WMError as exc:
            await ctx.reply_text("\n".join(filter(None, (notice, f"❌ 刷新卖单失败：{exc}"))), parse_mode=None)

    async def handle(self, ctx: Context, payload: str) -> None:
        parts = payload.split(":")
        if len(parts) not in (2, 3):
            await ctx.answer_query("按钮参数无效，请重新 /wmitems", show_alert=True)
            return
        key, action = parts[:2]
        view = self.views.get(key)
        if not view or time.monotonic() - view.created > VIEW_TTL:
            self.views.pop(key, None)
            await ctx.answer_query("卡片已更新或过期，请重新 /wmitems", show_alert=True)
            return
        if not ctx.user or ctx.user.id != view.owner:
            await ctx.answer_query("这是其他用户的卖单，请用 /wmitems 打开自己的目录", show_alert=True)
            return
        client = await wm()
        session = client.session_for(view.owner)
        if not session.logged_in or account_key(session) != view.account:
            await ctx.answer_query("绑定账号已变化，请重新 /wmitems", show_alert=True)
            return
        if view.account in self.busy:
            await ctx.answer_query("上一项操作仍在处理中，请稍后再点", show_alert=True)
            return
        if action in PRICE_LABELS and not view.targets.get(action):
            await ctx.answer_query("暂无这个参考价；原价重提和售出仍可使用", show_alert=True)
            return
        if action not in {*PRICE_LABELS, "sell1", "sellall", "bump", "page", "select", "refresh"}:
            await ctx.answer_query("未知操作，请重新 /wmitems", show_alert=True)
            return
        position = None
        if action in ("page", "select"):
            try:
                position = int(parts[2])
            except (IndexError, ValueError):
                await ctx.answer_query("商品位置无效，请重新 /wmitems", show_alert=True)
                return
            limit = view.last_page + 1 if action == "page" else len(view.orders)
            if not 0 <= position < limit:
                await ctx.answer_query("商品位置已过期，请重新 /wmitems", show_alert=True)
                return
        # 在第一个网络等待之前消费快照，重复投递不会重复售出。
        self.views.pop(key, None)
        self.busy.add(view.account)
        try:
            await ctx.answer_query()
            if action == "bump":
                await self._bump(ctx, client, session)
                await self.show(ctx, view.page, selected_id=view.order["id"], edit=True)
                return
            if action in ("page", "select", "refresh"):
                selected_id = view.orders[position]["id"] if action == "select" else view.order["id"]
                await self.show(ctx, position if action == "page" else view.page,
                                selected_id="" if action == "page" else selected_id,
                                edit=True, force=action == "refresh")
                return
            notice = await self._act(client, session, view, action)
            await self.show(ctx, view.page, selected_id=view.order["id"], edit=True, notice=notice)
        except WMError as exc:
            # 超时可能发生在远端已提交之后，旧按钮不恢复，不自动重试写请求。
            await self.show(ctx, view.page, selected_id=view.order["id"], edit=True,
                            notice=f"⚠️ 操作未获确认：{exc}。请核对最新挂单后再操作。")
        finally:
            self.busy.discard(view.account)

    async def _act(self, client, session, view: ListingView, action: str) -> str:
        orders = await client.get_my_orders(session)
        current = next((order for order in orders if order["id"] == view.order["id"]), None)
        if not current or order_signature(current) != order_signature(view.order):
            return "⏭️ 挂单价格、数量或状态已变化，本次未提交；已刷新卡片。"
        if action in ("sell1", "sellall"):
            quantity = 1 if action == "sell1" else current["quantity"]
            per_trade = max(1, int(current.get("per_trade") or 1))
            if quantity % per_trade:
                return f"⏭️ 该挂单每次交易 {per_trade} 件，售出数量须为其整数倍，本次未提交。"
            sale = await client.sell_order(current["id"], quantity, session, platinum=current["platinum"])
            if not sale.get("confirmed"):
                return "⚠️ 服务端已接受售出请求，未返回成交明细；请核对最新数量，勿直接重复提交。"
            return f"✅ 已售出 {sale['quantity']} 件，实际成交价 {sale['platinum']}p。"
        target = view.targets[action]
        if action != "keep":
            index = await self.ensure_items()
            targets, _ = await self._references(
                client, session, current, index.get_by_id(current["item_id"]),
                {order["id"] for order in orders}, force=True,
            )
            if targets.get(action) != target:
                return "⏭️ 参考价已变化，本次未提交；请按刷新后的价格操作。"
        # 原价也必须 PATCH，不能像一般改价逻辑那样跳过同价订单。
        await client.update_order_price(current["id"], target, session)
        return (f"✅ 已按原价 {target}p 重新提交。" if action == "keep"
                else f"✅ 已按{PRICE_LABELS[action]}改为 {target}p（原 {current['platinum']}p）。")

    async def bump(self, ctx: Context) -> None:
        client = await wm()
        session = client.session_for(int(ctx.user.id) if ctx.user else 0)
        if not session.logged_in:
            await ctx.reply_text("请先 /wmlogin 绑定账号")
            return
        account = account_key(session)
        if account in self.busy:
            await ctx.reply_text("上一项订单操作仍在处理中，请稍后再试。")
            return
        self.busy.add(account)
        try:
            await self._bump(ctx, client, session)
        except WMError as exc:
            await ctx.reply_text(f"❌ 读取卖单失败，未执行原价重提：{exc}")
        finally:
            self.busy.discard(account)

    async def _bump(self, ctx: Context, client, session) -> None:
        orders = await client.get_my_orders(session)
        sells = [order for order in orders if order.get("type") == "sell" and order.get("quantity", 0) > 0]
        if not sells:
            await ctx.reply_text("当前没有可重新提交的卖单。")
            return
        await ctx.reply_text(f"♻️ 正在按当前价格重新提交 {len(sells)} 条卖单，保留数量和可见状态…")
        success, failed, skipped = 0, 0, 0
        errors = []
        for position, order in enumerate(sells):
            try:
                await client.update_order_price(order["id"], order["platinum"], session)
                success += 1
            except WMError as exc:
                failed += 1
                errors.append(f"{order['id']}：{str(exc)[:120]}")
                if exc.kind in ("auth", "rate", "network"):
                    skipped = len(sells) - position - 1
                    break
        text = f"♻️ 原价重提完成：成功 {success} 条 · 未确认 {failed} 条 · 未尝试 {skipped} 条"
        if errors:
            text += "\n" + "\n".join(errors[:5]) + "\n未确认的订单请先核对，未自动重试。"
        await ctx.reply_text(text, parse_mode=None)
