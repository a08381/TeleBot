"""plugins/wm.py —— Warframe Market 托管：登录 / 改在线状态 / 查价（支持黑话）。

**一个 Telegram user id 绑定一个 WM 账号**：A 改状态不会动到 B 的账号，
会话按 user id 分开存在 config/wm_sessions.json（权限 0600）。

指令
----
    /wm                     面板：自己绑定的账号 + 状态按钮
    /wm online|ingame|invisible      直接改**自己**账号的状态
    /wmstatus [状态]        查看 / 改自己账号的状态
    /wmlogin [邮箱 密码]     绑定（不带参数 = 主人用 config/wm.json 里的账号）
    /wmlogout [tg用户id]     解绑（不带参数 = 解绑自己；带 id 需主人权限）
    /wmwho                  查看已绑定了哪些账号（主人）
    /price <物品> [platform=ps4] [crossplay=true] [rank=5]   查价（公开数据，无需登录）
    /wmrefresh              强制刷新物品清单（主人）
    /wmdiag                 逐个试探端点，看官方接口现在长什么样（主人）

查价例子（黑话）::

    /price 满级充沛            -> Arcane Energize，按 rank 5 过滤
    /price 咖喱p               -> Excalibur Prime Set
    /price 咖喱p 图纸          -> Excalibur Prime Blueprint
    /price nikana prime set platform=xbox crossplay=true
    /price 充沛 rank=3

为什么状态写入是「探测 + 回读」
------------------------------
官方 v2 文档只确认了 profile 的 PATCH，**没公开在线状态写入端点**，而 v1 的
``PUT /profile/status`` 属于社区沿用。所以这里两条路都试，写完再用
``GET /v2/me`` 复核；复核不一致就如实说「请求已接受，但状态未确认」，
绝不假装成功。文档没写死的东西，代码也不该写死。

状态语义（官方 FAQ）：ingame=可交易，online=约 1 小时内可交易，invisible=不可交易；
offline 是断线后的自动状态，所以不提供手动设置。
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from core import Context, Keyboard, button, get_config, listener, plugin_config, shutdown, startup
from utils import wm_api
from utils.wm_api import (
    DEFAULT_UA,
    RANK_MAX,
    SETTABLE_STATUSES,
    STATUS_LABEL,
    V1_BASE,
    V2_BASE,
    ITEM_PAGE,
    WMError,
    WMSession,
    configure,
    wm,
    wm_start,
    wm_stop,
)
from utils.wm_items import Item, ItemIndex, get_index, parse_query

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ 配置
# 首次运行自动生成 config/wm.json。
# 注意：这里**不复用** utils.http_pool（那是 yiff 用的单站点单例），
# wm 走 utils.wm_api 自己的客户端 —— 站点、UA、JWT 都不一样。
cfg = plugin_config({
    "_comment": "Warframe Market 插件配置；一个 Telegram user id 绑定一个 WM 账号",

    # ---- 主人的默认账号（/wmlogin 不带参数时用它） ----
    "account": {
        "email": "",
        "password": "",
        "user_id": 0,           # 绑到哪个 tg 用户；0 = 自动取 owner_ids 第一个
    },
    "autologin": True,          # 启动时若已填账号且该用户还没绑定就自动登录

    # ---- 谁可以绑定 ----
    "bind": {
        "allow_all": True,          # 允许任何人绑定**自己**的账号（false = 只有主人能绑）
        "owner_can_unbind_all": True,  # 主人可 /wmlogout <tg用户id> 解绑别人
    },

    # ---- 平台 ----
    "platform": "pc",           # pc / ps4 / xbox / switch / mobile
    "crossplay": False,
    "language": "en",
    "user_agent": DEFAULT_UA,   # 官方 Rules 要求：必须能标识你的应用

    # ---- 浏览器指纹 + Cookie（被 Cloudflare 拦时开） ----
    "browser": {
        "_comment": "impersonate 填 chrome124 等即启用 TLS 指纹（需 pip install curl_cffi）；cookies 可贴 cf_clearance",
        "impersonate": "",
        "sync_ua": True,
        "user_agent": "",
        "cookies": {},
        "cookie": "",
        "cookie_domains": [],
        "headers": {},
        "proxy": "",
        "timeout": 20.0,
        "verify": True,
    },

    # ---- 接口 ----
    "api": {
        "v1_base": V1_BASE,     # v1 仅用于登录与状态写入（官方说授权仍走 v1）
        "v2_base": V2_BASE,
        "timeout": 20.0,
        "auth_scheme": "auto",  # auto（v1 用 JWT、v2 用 Bearer）/ bearer / jwt
    },
    "rate_limit": {"rps": 3.0, "burst": 3.0},   # 官方公共限流 3 RPS
    "cache": {"items_ttl": 86400.0, "orders_ttl": 60.0},
    "session_file": "config/wm_sessions.json",  # 会话库：tg id -> 账号（含 JWT，权限 0600）
    "warm_items": True,          # 启动时预热物品清单（查黑话要用）

    # ---- 状态写入 ----
    "status_write": {
        "mode": "auto",         # auto（先 v2 再 v1）/ v2 / v1 / off
        "verify": True,         # 写完用 GET /v2/me 复核
    },

    # ---- 查价 ----
    "price": {
        "only_online": True,    # 只统计 online / ingame 的挂单
        "top_n": 5,             # 买卖各显示几条
        "group_by_rank": True,  # Mod/Arcane 没指定等级时，按等级各列一行
        "show_statistics": True,  # 追加 48h 历史统计（接口不通就自动省略）
        "send_thumb": True,     # 尝试带物品图标，失败自动退回纯文本
    },

    # ---- 黑话 ----
    # 键 = 玩家叫法，值 = 物品**基名**（不带 _prime / _set / _blueprint）。
    # 官方中英文名会自动入索引，所以这里只放官方名解释不了的昵称。
    # 命中不到也没关系：会退化成关键字搜索，让你在候选里选。
    "aliases": {
        "_comment": "黑话 -> 物品基名。可自行增删；值要写英文名去掉后缀的形式，例如 excalibur / arcane_energize",

        # 战甲
        "咖喱": "excalibur", "咖喱棒": "excalibur",
        "电男": "volt", "电王": "volt",
        "奶妈": "trinity", "奶": "trinity",
        "犀牛": "rhino", "牛": "rhino",
        "摸尸": "nekros", "摸": "nekros",
        "玻璃": "gara",
        "女枪": "mesa",
        "蝴蝶": "titania",
        "鸟姐": "zephyr", "鸟": "zephyr",
        "火鸡": "ember", "火女": "ember",
        "毒妈": "saryn", "毒": "saryn",
        "沙王": "inaros", "沙甲": "inaros", "沙": "inaros",
        "阴阳": "equinox", "阴阳人": "equinox",
        "冰男": "frost", "冰": "frost",
        "水男": "hydroid", "水": "hydroid",
        "悟空": "wukong", "猴": "wukong", "猴子": "wukong",
        "磁力": "mag",
        "龙": "chroma", "龙甲": "chroma",
        "鹿": "oberon", "鹿甲": "oberon",
        "石头": "atlas", "岩": "atlas",
        "血妈": "garuda",
        "高斯": "gauss",
        "章鱼": "octavia", "章鱼姐": "octavia",

        # 赋能（Arcane）
        "充沛": "arcane_energize",
        "复仇": "arcane_avenger",
        "守护": "arcane_guardian",
        "生机": "arcane_vitality",
        "壁垒": "arcane_barrier",
        "坚持": "arcane_persistence",
    },
})


def _migrate_session_file() -> None:
    """老配置的 session_file 指向单账号文件；名字改成会话库，语义才对得上。

    值已经被人手改过就尊重它，只动那个旧默认值。
    """
    raw = str(cfg.get("session_file") or "").strip()
    if raw == "config/wm_session.json":
        cfg.set("session_file", "config/wm_sessions.json")
        logger.info("会话存储已切换到多账号格式：config/wm_sessions.json（旧会话需重新 /wmlogin）")


def _client_settings() -> dict:
    return {
        "user_agent": cfg.get("user_agent") or DEFAULT_UA,
        "platform": cfg.get("platform") or "pc",
        "language": cfg.get("language") or "en",
        "crossplay": bool(cfg.get("crossplay")),
        "browser": cfg.get("browser"),
        "session_file": cfg.get("session_file") or "config/wm_sessions.json",
        "api": cfg.get("api"),
        "rate_limit": cfg.get("rate_limit"),
        "cache": cfg.get("cache"),
    }


# 同 yiff.configure_site()：模块 import 时就登记参数，早于任何请求。
_migrate_session_file()
configure(_client_settings())

# 候选物品的短 key（callback_data 有 64 字节上限，slug 可能超）
_PICKS: dict[str, tuple[str, Optional[int]]] = {}

CB_STATUS = "wm:st:"
CB_ME = "wm:me"
CB_LOGOUT = "wm:logout"
CB_PICK = "wm:pick:"


# ------------------------------------------------------------------ 工具
def _owner_only(ctx: Context) -> bool:
    user = ctx.user
    return bool(user and user.is_owner)


def _tg_id(ctx: Context) -> int:
    user = ctx.user
    return int(user.id) if user else 0


def _can_bind(ctx: Context) -> bool:
    """是否允许绑定自己的账号。"""
    if _owner_only(ctx):
        return True
    return bool((cfg.get("bind") or {}).get("allow_all", True))


def _owner_account_user_id() -> int:
    """配置里主人账号要绑到谁身上；没填就取 owner_ids 第一个。"""
    configured = (cfg.get("account") or {}).get("user_id") or 0
    try:
        configured = int(configured)
    except (TypeError, ValueError):
        configured = 0
    if configured:
        return configured
    owners = get_config().owner_ids or ()
    for owner in owners:
        try:
            return int(owner)
        except (TypeError, ValueError):
            continue
    return 0


def _median(values: list[int]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2


def _fmt_money(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return f"{int(value)}" if float(value).is_integer() else f"{value:.1f}"


def _status_icon(status: str) -> str:
    return {"ingame": "🎮", "online": "🟢", "invisible": "👻", "offline": "⚪️"}.get(status, "❔")


async def _ensure_items(force: bool = False) -> ItemIndex:
    """拿到物品索引（黑话解析的前提）。清单默认缓存 24h。"""
    index = get_index()
    index.set_aliases(cfg.get("aliases"))      # 别名随时可能改，每次刷新
    if force or len(index) == 0:
        client = await wm()
        raw = await client.get_items(force=force)
        index.load(raw)
        logger.info("WM 物品清单已载入：%d 条", len(index))
    return index


# ------------------------------------------------------------------ 面板
def _panel_keyboard() -> Keyboard:
    return (
        Keyboard()
        .callback("🟢 在线", f"{CB_STATUS}online")
        .callback("🎮 游戏中", f"{CB_STATUS}ingame")
        .callback("👻 隐身", f"{CB_STATUS}invisible")
        .row()
        .callback("📊 我的状态", CB_ME)
        .callback("🚪 解绑", CB_LOGOUT)
    )


@listener("wm")
async def wm_panel(ctx: Context, *args, **kwargs):
    """面板；带 online/ingame/invisible 参数时直接改状态。"""
    sub = (args[0] if args else "").strip().lower()
    if sub in SETTABLE_STATUSES:
        await _do_set_status(ctx, sub)
        return

    try:
        client = await wm()
    except WMError as exc:
        await ctx.reply_text(f"客户端未就绪：{exc}")
        return

    session = client.session_for(_tg_id(ctx))
    lines = ["🌐 Warframe Market"]
    if session.logged_in:
        lines.append(f"已绑定：{session.label}")
        if session.status:
            lines.append(f"当前状态：{STATUS_LABEL.get(session.status, session.status)}")
    else:
        lines.append("还没绑定账号 —— 发 /wmlogin 邮箱 密码 绑定你自己的 WM 账号")
        if _owner_only(ctx):
            lines.append("（主人也可以把账号填进 config/wm.json 的 account 段再 /wmlogin）")

    try:
        index = await _ensure_items()
        lines.append(f"物品索引：{len(index)} 条")
    except WMError as exc:
        lines.append(f"物品索引：未载入（{exc}）")

    lines.append("")
    lines.append("查价：/price 满级充沛 · /price 咖喱p 图纸")
    lines.append("状态：/wmstatus ingame · 或直接 /wm ingame")

    await ctx.reply_text("\n".join(lines), reply_markup=_panel_keyboard())


# ------------------------------------------------------------------ 登录 / 绑定
@listener("wmlogin")
async def wm_login(ctx: Context, *args, **kwargs):
    """绑定 WM 账号到**当前** Telegram 用户。"""
    if not _can_bind(ctx):
        await ctx.reply_text("当前不允许普通用户绑定 WM 账号（config/wm.json 的 bind.allow_all）")
        return

    # 密码出现在聊天记录里不好，能删就删
    async def _wipe() -> None:
        try:
            if ctx.message and ctx.message.chat_id:
                await ctx.bot.delete_message(chat_id=ctx.message.chat_id, message_id=ctx.message.id)
        except Exception:
            pass

    email = args[0] if len(args) > 0 else ""
    password = args[1] if len(args) > 1 else ""

    if not email or not password:
        # 不带参数：只能用主人在配置里填的账号，且绑到配置指定的用户
        if not _owner_only(ctx):
            await ctx.reply_text(
                "用法：/wmlogin 邮箱 密码\n"
                "（不带参数时用的是主人在 config/wm.json 里预设的账号，只有主人能用）"
            )
            return
        account = cfg.get("account") or {}
        email = str(account.get("email") or "").strip()
        password = str(account.get("password") or "")
        if not email or not password:
            await ctx.reply_text(
                "用法：/wmlogin 邮箱 密码\n"
                "或把邮箱密码填进 config/wm.json 的 account 段再 /wmlogin（不进聊天记录）"
            )
            return
        target = _owner_account_user_id()
    else:
        await _wipe()
        target = _tg_id(ctx)

    if not target:
        await ctx.reply_text("无法确定要绑定到哪个 Telegram 用户，请联系主人检查配置")
        return

    try:
        client = await wm()
        session = await client.login(email, password, tg_user_id=target)
        name = session.ingame_name or "(未取到昵称)"
        await ctx.reply_text(
            f"✅ 已绑定 Warframe Market\n账号：{name}"
            + (f"\n当前状态：{STATUS_LABEL.get(session.status, session.status)}" if session.status else "")
            + (f"\n绑定到：{target}" if target != _tg_id(ctx) else "")
        )
    except WMError as exc:
        hint = ""
        if exc.kind == "auth":
            hint = "\n（若开了 2FA 或触发了验证码，WM 可能拒绝无头登录）"
        await ctx.reply_text(f"❌ 登录失败：{exc}{hint}")


@listener("wmlogout")
async def wm_logout(ctx: Context, *args, **kwargs):
    """解绑自己；主人可以指定 tg 用户 id 解绑别人。"""
    target = _tg_id(ctx)
    if args:
        if not _owner_only(ctx):
            await ctx.reply_text("只有主人可以解绑别人的账号")
            return
        if not (cfg.get("bind") or {}).get("owner_can_unbind_all", True):
            await ctx.reply_text("主人解绑他人已关闭（config/wm.json 的 bind.owner_can_unbind_all）")
            return
        try:
            target = int(str(args[0]).strip())
        except ValueError:
            await ctx.reply_text("用法：/wmlogout [tg用户id]")
            return

    client = await wm()
    session = client.session_for(target)
    if not session.logged_in and target not in client.store.all():
        await ctx.reply_text("该用户没有绑定账号")
        return
    label = session.label
    await client.logout(session)
    await ctx.reply_text(f"🚪 已解绑 {label}（tg {target} 的本地会话已清除）")


@listener("wmwho")
async def wm_who(ctx: Context, *args, **kwargs):
    """查看已绑定了哪些账号（不显示 token）。"""
    client = await wm()
    bound = client.bound_accounts()
    if not bound:
        await ctx.reply_text("还没有任何用户绑定 WM 账号")
        return

    # 非主人只准看自己
    if not _owner_only(ctx):
        mine = client.session_for(_tg_id(ctx))
        if not mine.logged_in:
            await ctx.reply_text("你还没绑定 WM 账号，发 /wmlogin 绑定")
            return
        await ctx.reply_text(
            f"你的绑定：{mine.label}"
            + (f"\n状态：{STATUS_LABEL.get(mine.status, mine.status)}" if mine.status else "")
        )
        return

    lines = [f"已绑定 {len(bound)} 个账号：", ""]
    for session in bound:
        when = time.strftime("%m-%d %H:%M", time.localtime(session.bound_at)) if session.bound_at else "-"
        lines.append(f"· tg {session.tg_user_id} → {session.label}（{when}）")
    await ctx.reply_text("\n".join(lines))


# ------------------------------------------------------------------ 状态
async def _do_set_status(ctx: Context, status: str, *, session: Optional[WMSession] = None) -> None:
    status = (status or "").strip().lower()
    try:
        client = await wm()
    except WMError as exc:
        await ctx.reply_text(f"客户端未就绪：{exc}")
        return

    session = session or client.session_for(_tg_id(ctx))
    if not session.logged_in:
        await ctx.reply_text("你还没绑定 WM 账号，先 /wmlogin 邮箱 密码")
        return

    mode = str((cfg.get("status_write") or {}).get("mode") or "auto")
    verify = bool((cfg.get("status_write") or {}).get("verify", True))
    try:
        result = await client.set_status(status, session, mode=mode, verify=verify)
    except WMError as exc:
        hint = ""
        if exc.kind == "auth":
            hint = "\n凭证可能过期了，重新 /wmlogin"
        else:
            hint = "\n提示：官方 v2 未公开状态写入端点；试试 config/wm.json 里 status_write.mode 改成 v1 或 v2"
        await ctx.reply_text(f"❌ 设置失败：{exc}{hint}")
        return

    label = STATUS_LABEL.get(status, status)
    if result.get("confirmed"):
        await ctx.reply_text(f"✅ {session.label} 状态已更新：{label}（经 {result.get('via')} 写入并回读确认）")
    elif result.get("verified"):
        await ctx.reply_text(
            f"⚠️ 请求已接受，但回读到的状态是 {result.get('verified')}（目标 {status}）\n"
            f"写入路径：{result.get('via')}"
        )
    else:
        await ctx.reply_text(f"⚠️ 请求已发出（经 {result.get('via')}），但没能回读确认，稍后用 /wmstatus 查看")


@listener("wmstatus")
async def wm_status(ctx: Context, *args, **kwargs):
    target = (args[0] if args else "").strip().lower()
    if target:
        await _do_set_status(ctx, target)
        return

    try:
        client = await wm()
    except WMError as exc:
        await ctx.reply_text(f"客户端未就绪：{exc}")
        return

    session = client.session_for(_tg_id(ctx))
    if not session.logged_in:
        await ctx.reply_text("你还没绑定 WM 账号，先 /wmlogin 邮箱 密码")
        return

    try:
        me = await client.get_me(session)
    except WMError as exc:
        await ctx.reply_text(f"❌ 读取状态失败：{exc}")
        return

    status = str(me.get("status") or "")
    lines = [
        f"👤 {me.get('ingameName') or session.ingame_name or '(未知)'}",
        f"状态：{STATUS_LABEL.get(status, status or '未知')}",
    ]
    if me.get("activity"):
        lines.append(f"活动：{me.get('activity')}")
    if me.get("lastSeen"):
        lines.append(f"最后在线：{me.get('lastSeen')}")
    if me.get("platform"):
        lines.append(f"平台：{me.get('platform')}（跨平台：{'是' if me.get('crossplay') else '否'}）")
    await ctx.reply_text("\n".join(lines), reply_markup=_panel_keyboard())


# ------------------------------------------------------------------ 查价
def _filter_orders(orders: list[dict], *, rank: Optional[int], only_online: bool) -> list[dict]:
    out = []
    for order in orders:
        if order.get("visible") is False:
            continue
        if rank is not None and int(order.get("rank") or 0) != rank:
            continue
        if only_online and order.get("user_status") not in wm_api.ONLINE_STATUSES:
            continue
        out.append(order)
    return out


def _order_line(order: dict) -> str:
    icon = _status_icon(order.get("user_status") or "")
    name = order.get("user") or "(匿名)"
    return f"  {order['platinum']:>3}p ×{order['quantity']}  {name} {icon}".rstrip()


def _rank_block(orders: list[dict], item: Item) -> str:
    """Mod / Arcane：按等级各列一行最低卖价。"""
    sells = [o for o in orders if o.get("type") == "sell"]
    rows: list[str] = []
    for rank in range(0, item.max_rank + 1):
        prices = [o["platinum"] for o in sells if int(o.get("rank") or 0) == rank]
        if not prices:
            continue
        rows.append(f"  r{rank}  最低 {min(prices)}p（{len(prices)} 单）")
    return "\n".join(rows) if rows else "  （当前没有符合的挂单）"


async def _show_price(ctx: Context, item: Item, *, rank: Optional[int],
                      platform: Optional[str], crossplay: Optional[bool]) -> None:
    client = await wm()
    try:
        orders = await client.get_orders(item.slug, platform=platform, crossplay=crossplay)
    except WMError as exc:
        await ctx.reply_text(f"❌ 查询失败：{exc}")
        return

    price_cfg = cfg.get("price") or {}
    only_online = bool(price_cfg.get("only_online", True))
    top_n = max(1, int(price_cfg.get("top_n") or 5))

    # 满级：把哨兵换成具体等级；普通物品没有等级概念就忽略
    note = ""
    if rank == RANK_MAX:
        if item.max_rank > 0:
            rank = item.max_rank
        else:
            rank = None
            note = "（该物品没有等级概念，已忽略「满级」）"

    filtered = _filter_orders(orders, rank=rank, only_online=only_online)
    sells = sorted((o for o in filtered if o.get("type") == "sell"), key=lambda o: o["platinum"])
    buys = sorted((o for o in filtered if o.get("type") == "buy"), key=lambda o: o["platinum"], reverse=True)

    plat = platform or client.platform
    cross = client.crossplay if crossplay is None else crossplay
    lines = [f"🔎 {item.display}"]
    lines.append(f"   {item.slug} · {plat} · 跨平台 {'开' if cross else '关'}"
                 + (" · 仅在线/游戏中" if only_online else ""))
    if rank is not None:
        lines.append(f"   等级：r{rank}" + ("（满级）" if rank == item.max_rank else ""))
    if note:
        lines.append(f"   {note}")

    if item.has_rank and rank is None and price_cfg.get("group_by_rank", True):
        lines.append("")
        lines.append("按等级（卖单最低价）")
        lines.append(_rank_block(filtered, item))

    if sells:
        med = _median([o["platinum"] for o in sells])
        lines.append("")
        lines.append(f"💰 卖单（最低 {min(top_n, len(sells))} / 共 {len(sells)}，中位 {_fmt_money(med)}p）")
        lines.extend(_order_line(o) for o in sells[:top_n])
    if buys:
        med = _median([o["platinum"] for o in buys])
        lines.append("")
        lines.append(f"🧾 买单（最高 {min(top_n, len(buys))} / 共 {len(buys)}，中位 {_fmt_money(med)}p）")
        lines.extend(_order_line(o) for o in buys[:top_n])
    if not sells and not buys:
        lines.append("")
        lines.append("当前没有符合条件的挂单（可试试关掉 only_online 或换个平台/等级）")

    # 有等级的物品必须指定了 rank 才看统计：混着各等级的成交价没有意义
    if price_cfg.get("show_statistics", True) and (not item.has_rank or rank is not None):
        try:
            stats = await client.get_statistics(item.slug)
        except WMError:
            stats = {}
        bucket = stats.get("48hours") or {}
        if bucket:
            lines.append("")
            lines.append(
                "📈 近 48h：中位 {median}p · 均价 {avg}p · 区间 {low}~{high}p · 成交 {vol} 笔".format(
                    median=_fmt_money(bucket.get("median")),
                    avg=_fmt_money(bucket.get("avg")),
                    low=_fmt_money(bucket.get("min")),
                    high=_fmt_money(bucket.get("max")),
                    vol=bucket.get("volume") if bucket.get("volume") is not None else "-",
                )
            )

    keyboard = Keyboard().url("🔗 WM 页面", ITEM_PAGE.format(slug=item.slug))

    text = "\n".join(lines)
    if price_cfg.get("send_thumb", True) and item.thumb:
        try:
            await ctx.reply_photo(wm_api.thumb_url(item.thumb), caption=text, reply_markup=keyboard)
            return
        except Exception as exc:
            logger.info("带图标发送失败，退回纯文本: %s", exc)
    await ctx.reply_text(text, reply_markup=keyboard)


def _store_pick(item: Item, rank: Optional[int]) -> str:
    import uuid

    key = uuid.uuid4().hex[:8]
    _PICKS[key] = (item.slug, rank)
    if len(_PICKS) > 300:
        for old in list(_PICKS)[:150]:
            _PICKS.pop(old, None)
    return key


async def _show_candidates(ctx: Context, candidates: list[Item], *, rank: Optional[int]) -> None:
    keyboard = Keyboard()
    for item in candidates[:8]:
        key = _store_pick(item, rank)
        label = item.display if len(item.display) <= 28 else item.display[:27] + "…"
        keyboard.callback(label, f"{CB_PICK}{key}").row()
    await ctx.reply_text("没找到唯一匹配，你想查的是哪个？", reply_markup=keyboard)


@listener("price")
@listener("wmprice")
async def wm_price(ctx: Context, *args, **kwargs):
    if not args:
        await ctx.reply_text(
            "用法：/price <物品> [platform=ps4] [crossplay=true] [rank=5]\n"
            "例：/price 满级充沛 · /price 咖喱p 图纸 · /price nikana prime set"
        )
        return

    text = " ".join(str(a) for a in args)
    parsed = parse_query(text)

    # k=v 形式覆盖（/price 充沛 rank=3 platform=ps4）
    platform = kwargs.get("platform") or kwargs.get("plat")
    crossplay_raw = kwargs.get("crossplay")
    crossplay = None if crossplay_raw is None else str(crossplay_raw).lower() in ("1", "true", "yes", "on")
    rank_raw = kwargs.get("rank", kwargs.get("r"))
    if rank_raw is not None:
        lowered = str(rank_raw).lower()
        if lowered in ("max", "满级", "满"):
            parsed.rank = RANK_MAX
        else:
            try:
                parsed.rank = int(lowered)
            except ValueError:
                pass

    try:
        index = await _ensure_items()
    except WMError as exc:
        await ctx.reply_text(f"❌ 物品清单没载入：{exc}\n（多半是接口被拦，试试 /wmdiag 看看）")
        return

    item, candidates = index.resolve(parsed)
    if item is None:
        if candidates:
            await _show_candidates(ctx, candidates, rank=parsed.rank)
        else:
            await ctx.reply_text(
                f"没找到「{text}」对应的物品。\n"
                "· 换个官方名试试（英文更稳，如 arcane energize）\n"
                "· 也可以把常用叫法加进 config/wm.json 的 aliases 段"
            )
        return

    await _show_price(ctx, item, rank=parsed.rank, platform=platform, crossplay=crossplay)


# ------------------------------------------------------------------ 诊断 / 刷新
@listener("wmrefresh")
async def wm_refresh(ctx: Context, *args, **kwargs):
    if not _owner_only(ctx):
        await ctx.reply_text("只有主人可以刷新")
        return
    try:
        index = await _ensure_items(force=True)
        await ctx.reply_text(f"✅ 物品清单已刷新：{len(index)} 条")
    except WMError as exc:
        await ctx.reply_text(f"❌ 刷新失败：{exc}")


@listener("wmdiag")
async def wm_diag(ctx: Context, *args, **kwargs):
    """逐个试探端点 —— 官方 v2 还在 <1.0，接口说变就变，先看清现状。"""
    if not _owner_only(ctx):
        await ctx.reply_text("只有主人可以诊断")
        return
    client = await wm()
    session = client.session_for(_tg_id(ctx))
    await ctx.reply_text("🔧 正在试探各端点（每个都有超时，稍等）…")
    started = time.monotonic()
    try:
        checks = await client.probe(session)
    except WMError as exc:
        await ctx.reply_text(f"❌ 诊断失败：{exc}")
        return

    lines = [f"端点探测（用时 {time.monotonic() - started:.1f}s）", ""]
    for name, ok, detail in checks:
        lines.append(f"{'✅' if ok else '❌'} {name} —— {detail}")
    lines.append("")
    lines.append(f"你的账号：{session.label if session.logged_in else '未绑定'}")
    lines.append(f"已绑定账号数：{len(client.bound_accounts())}")
    lines.append(f"指纹：{client.browser.impersonate or '无'}"
                 f"{'' if client.browser.enabled else '（未生效：缺 curl_cffi）' if client.browser.impersonate else ''}")
    lines.append(f"UA：{client.browser.effective_ua(client.user_agent)}")
    await ctx.reply_text("\n".join(lines))


# ------------------------------------------------------------------ 按钮
@button(CB_STATUS)
async def on_status_button(ctx: Context, status: str):
    await ctx.answer_query()
    await _do_set_status(ctx, status)          # 用**点击者**的账号，不是发消息的人


@button(CB_ME)
async def on_me_button(ctx: Context, _payload: str = ""):
    await ctx.answer_query()
    await wm_status(ctx)


@button(CB_LOGOUT)
async def on_logout_button(ctx: Context, _payload: str = ""):
    await ctx.answer_query()
    client = await wm()
    session = client.session_for(_tg_id(ctx))
    if not session.logged_in:
        await ctx.answer_query("你还没绑定账号", show_alert=True)
        return
    await client.logout(session)
    await ctx.answer_query("已解绑", show_alert=True)


@button(CB_PICK)
async def on_pick_button(ctx: Context, key: str):
    await ctx.answer_query()
    picked = _PICKS.get(key)
    if picked is None:
        await ctx.answer_query("这条按钮已过期，请重新发送指令", show_alert=True)
        return
    slug, rank = picked
    index = get_index()
    item = index.get(slug)
    if item is None:
        await ctx.answer_query("物品已不在索引里，请 /wmrefresh", show_alert=True)
        return
    await _show_price(ctx, item, rank=rank, platform=None, crossplay=None)


# ------------------------------------------------------------------ 生命周期
async def _auto_login() -> Optional[str]:
    """把配置里主人的账号绑到指定用户；已绑过就跳过。返回错误信息或 None。"""
    account = cfg.get("account") or {}
    email = str(account.get("email") or "").strip()
    password = str(account.get("password") or "")
    if not email or not password:
        return None
    target = _owner_account_user_id()
    if not target:
        return "配置里填了账号，但无法确定要绑定到哪个 Telegram 用户"

    client = await wm()
    if client.session_for(target).logged_in:
        return None
    try:
        session = await client.login(email, password, tg_user_id=target)
        return None if session.logged_in else "登录未返回凭证"
    except WMError as exc:
        return str(exc)


@startup
async def _start_wm(application=None) -> None:
    await wm_start(application)                 # 建客户端（配置已在 import 时登记）

    if cfg.get("autologin"):
        error = await _auto_login()
        if error:
            logger.warning("WM 自动登录失败: %s", error)

    if cfg.get("warm_items"):
        try:
            await _ensure_items()
        except WMError as exc:
            logger.warning("WM 物品清单预热失败（首次查价时会重试）: %s", exc)


@shutdown
async def _stop_wm(application=None) -> None:
    await wm_stop(application)
