"""plugins/yiff.py —— 发送带内联按钮的图片。

本插件独占一份配置：config/yiff.json（首次加载自动生成）。
e621/e926 相关的**全部**参数都在这里 —— 站点地址、默认标签、限流、图池容量……
主配置 config.json 里只剩下 FlareSolverr 那段（它属于浏览器基础设施，不是图站业务）。

按钮分两类：
- 「原图链接 / 帖子页」是 URL 按钮，纯跳转，不产生 callback
- 「换一张」是 callback 按钮，由 core.dispatcher 按 Buttons 注册表分发，
  插件里只需要 @button 声明，不需要在入口文件里注册 handler

发送策略（e621 的图可能是 webm/超大图，Telegram 对 photo 有格式和体积限制）：
1. 先试 reply_photo  → 能预览，体验最好
2. 失败则 reply_document → 支持任意格式
3. 仍失败则自己下载字节再发 → 绕开 Telegram 侧下载被 e926 拒绝的情况

取图通道开关（本插件独有，见 flaresolverr.enabled）：
- true  走 FlareSolverr：先解 Cloudflare 挑战，带着 cf_clearance 访问
- false 纯直连：普通 httpx 请求，只带 UA，不碰 FlareSolverr
两种切法：
- 改 config/yiff.json 的 flaresolverr.enabled，然后 /reload
- 主人直接发 /fs on、/fs off，运行时立即生效并写回配置文件

过 CF 还需要「浏览器指纹」（config/yiff.json 的 browser 段）：
cf_clearance 只证明"有人用浏览器解过挑战"，CF 同时还在看 TLS/HTTP2 指纹，
httpx 的握手一看就是脚本。填 browser.impersonate（chrome124 等）后请求会由
curl_cffi 发出，指纹与真实浏览器一致；自定义 Cookie 也在这段里配。
没装 curl_cffi 时自动退回 httpx，日志会提示，功能本身不受影响。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import PurePosixPath
from typing import Optional

from core import BotError, Context, Keyboard, button, listener, plugin_config, shutdown, startup
from utils.browser_profile import BrowserProfile
from utils.fs_pool import configure_site, fs, fs_health, set_mode
from utils.posts_pool import PoolSettings, Post, get_pool, pool_start, pool_stop

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ 配置
# 首次运行自动生成 config/yiff.json。
# 站点与图池参数在 startup 钩子里用于建池，改动后需要重启进程（池已按旧参数建好）；
# 其余参数（按钮、文案、体积上限）改完 /reload 即可生效。
cfg = plugin_config({
    # ---- 站点 ----
    "site": "https://e621.net",
    "default_tags": ["-intersex", "-female", "male", "order:rank"],
    "pool": {
        "size": 40,                # 预热池容量
        "low_water": 10,           # 低于此值就补货
        "refill_interval": 30.0,   # 补货检查间隔（秒）
        "min_interval": 1.1,       # e621 硬限 2 req/s，官方建议持续 <=1 req/s
        "cache_ttl": 300.0,        # 搜索结果缓存时长（秒）
    },

    # ---- 过 Cloudflare 挑战 ----
    # entry_url / session_name 不需要配：默认拿上面的 site，session 名取站点域名。
    # 只有 UA 得自己填（e621/e926 禁止浏览器 UA，但挑战又必须由浏览器过）。
    # FlareSolverr 客户端本身的参数（地址、超时、重试）在主配置 config.json。
    "flaresolverr": {
        "_comment": "enabled=false 时直连站点（不带 cf_clearance，也不连 FlareSolverr）；true 时走 FlareSolverr 解挑战。可用 /fs on|off 运行时切换",
        "enabled": False,               # 取图通道开关：true=FlareSolverr，false=直连
        "user_agent": "MyTgBot/1.0 (by a08381 on e621)",
    },

    # ---- 浏览器指纹 + 自定义 Cookie ----
    # cf_clearance 只能证明「解挑战的那台浏览器」，CF 还会看 TLS/HTTP2 指纹：
    # httpx 的握手形状一看就是脚本，所以想稳过 CF 得让请求也像浏览器。
    # impersonate 填 chrome124 / chrome / firefox135 / safari184 等即启用
    # （依赖 pip install curl_cffi，没装就自动退回 httpx，不会报错）。
    "browser": {
        "_comment": "impersonate 留空=不启用指纹；启用后 UA 会自动换成对应浏览器的 UA（cf_clearance 与 UA 绑定，必须一致）。cookies/cookie 二选一，写站点 Cookie（登录态、手填的 cf_clearance 等）",
        "impersonate": "",              # 浏览器指纹：chrome124 / firefox135 / safari184 ...
        "sync_ua": True,                # 启用指纹时，把指纹 UA 同步给 FlareSolverr 与请求头
        "user_agent": "",               # 手动指定 UA（优先于自动推导，自己保证与指纹一致）
        "cookies": {},                  # {"cf_clearance": "xxx", "login": "yyy"}
        "cookie": "",                   # 或直接贴字符串："a=1; b=2"
        "cookie_domains": [],           # 额外要发 Cookie 的域名（图片在 CDN 子域时填它）
        "headers": {},                  # 附加请求头，如 {"Referer": "https://e621.net/"}
        "proxy": "",                    # 留空则用 FlareSolverr 通道的代理设置
        "verify": True,                 # 关掉可跳过证书校验（自签/中间人时用，不建议）
    },

    # ---- 发送 ----
    "upload_limit_mb": 20,         # 自下载后上传的体积上限（MB）
    "photo_exts": [".jpg", ".jpeg", ".png", ".webp", ".gif"],   # 走 photo 而非 document 的扩展名
    "enable_next_button": True,    # 是否挂「换一张」按钮
    "caption": "帖子 {id}",        # 支持 {id} 占位符
})


def _fs_cfg() -> dict:
    """config/yiff.json 的 flaresolverr 段（副本，改它不会落盘）。"""
    return dict(cfg.get("flaresolverr") or {})


def _fs_enabled() -> bool:
    """当前是否走 FlareSolverr（默认开）。"""
    return bool(_fs_cfg().get("enabled", True))


def _save_fs_enabled(enabled: bool) -> None:
    """把开关写回 config/yiff.json，重启后依然是这个值。"""
    merged = _fs_cfg()
    merged["enabled"] = bool(enabled)
    cfg.set("flaresolverr", merged)


# 把站点信息交给取图客户端单例。必须在模块顶层执行：
# 插件 import（load_all_plugins）早于 post_init，fs() 首次调用时参数已就位。
# 改了 site / UA / enabled / browser 后 /reload 即可 —— fs() 发现站点、通道或
# 浏览器身份变了会重建客户端。
# entry_url / session_name 一般不用管，需要单独指定时往 config/yiff.json 的
# flaresolverr 段里加这两个键即可（plugin_config 不会删掉多出来的字段）。
def _configure_site() -> None:
    fs_cfg = _fs_cfg()
    configure_site(
        cfg.get("site") or "",
        user_agent=fs_cfg.get("user_agent"),
        entry_url=fs_cfg.get("entry_url"),
        session_name=fs_cfg.get("session_name"),
        source="yiff",
        enabled=_fs_enabled(),
        browser=cfg.get("browser"),     # 浏览器指纹 + 自定义 Cookie
    )


_configure_site()

# ---------- 换一张：callback_data 有 64 字节上限，这里只存短 key ----------
# key -> tags，进程内缓存；重启即失效，无所谓
_NEXT_TAGS: dict[str, list[str]] = {}
_NEXT_LOCK: Optional[asyncio.Lock] = None
CB_PREFIX = "yiff:next:"


def _get_lock() -> asyncio.Lock:
    global _NEXT_LOCK
    if _NEXT_LOCK is None:
        _NEXT_LOCK = asyncio.Lock()
    return _NEXT_LOCK


def _remember_tags(tags: list[str]) -> str:
    key = uuid.uuid4().hex[:8]
    _NEXT_TAGS[key] = tags
    # 简单防止无界增长
    if len(_NEXT_TAGS) > 500:
        for k in list(_NEXT_TAGS)[:200]:
            _NEXT_TAGS.pop(k, None)
    return key


def _default_tags() -> list[str]:
    return list(cfg.get("default_tags") or [])


def build_keyboard(page_url: str, file_url: str, next_key: Optional[str] = None) -> Keyboard:
    """按钮布局：第一行跳转类，第二行交互类。"""
    keyboard = Keyboard().url("🔗 帖子页", page_url).url("🖼 原图", file_url)
    if cfg.get("enable_next_button") and next_key:
        keyboard.row().callback("🎲 换一张", f"{CB_PREFIX}{next_key}")
    return keyboard


def _is_photo(url: str) -> bool:
    exts = {e.lower() for e in cfg.get("photo_exts") or []}
    return PurePosixPath(url.split("?")[0]).suffix.lower() in exts


def _filename(post: Post) -> str:
    ext = PurePosixPath(post.file_url.split("?")[0]).suffix or ".png"
    return f"{post.id}{ext}"


async def _send_media(ctx: Context, post: Post, markup: Keyboard) -> None:
    """photo -> document -> 自下载字节，三级回退。"""
    # 纯文本 caption：不设 parse_mode 时 Markdown 方括号会原样显示
    caption = cfg.get("caption").format(id=post.id)

    # 1) photo（仅常见图片格式）
    if _is_photo(post.file_url):
        try:
            await ctx.reply_photo(post.file_url, caption=caption, reply_markup=markup)
            return
        except BotError as e:
            logger.info("send_photo 失败，回退 document: %s", e)

    # 2) document（URL 直传）
    try:
        await ctx.reply_document(
            post.file_url, caption=caption, filename=_filename(post), reply_markup=markup
        )
        return
    except BotError as e:
        logger.info("send_document 失败，改为自己下载: %s", e)

    # 3) 自己下载字节上传（走当前通道的客户端，绕开 e926 对 Telegram 的拒绝）
    client = await fs()
    resp = await client.get(post.file_url)
    resp.raise_for_status()
    data = resp.content
    limit = float(cfg.get("upload_limit_mb") or 0) * 1024 * 1024
    if limit and len(data) > limit:
        raise BotError(f"文件过大（{len(data) / 1024 / 1024:.1f}MB），跳过")
    await ctx.reply_document(
        data, caption=caption, filename=_filename(post), reply_markup=markup
    )


@listener("yiff")
async def yiff(ctx: Context, *args, **kwargs):
    tags = list(args) or _default_tags()
    try:
        pool = get_pool()
        if pool is None:
            await ctx.reply_text("图池尚未就绪，请稍后再试")
            return

        post = await pool.random_post(tags)
        if post is None:
            await ctx.reply_text("未能找到所需图片")
            return

        async with _get_lock():
            next_key = _remember_tags(tags)

        markup = build_keyboard(post.page_url, post.file_url, next_key)
        await _send_media(ctx, post, markup)

    except BotError as e:
        logger.warning("发送图片失败: %s", e)
        await ctx.reply_text(f"发送失败：{e}")
    except Exception:
        logger.exception("yiff 指令异常")
        await ctx.reply_text("出错了，请稍后再试")


# --------------------------------------------------------------------------
# 「换一张」回调处理
#
# callback_data 以 "yiff:next:" 开头就会命中这里，由 core.dispatcher 分发，
# 入口文件不需要为它单独 add_handler。
# 注意：reload_all_plugins() 会 pop 掉 plugins.* 模块，重新 import 后函数对象会变，
# 但分发是按注册表动态查找的，所以热重载后新函数会自动生效。
# --------------------------------------------------------------------------
@button(CB_PREFIX)
async def yiff_next(ctx: Context, key: str) -> None:
    await ctx.answer_query()                   # 必须先应答，否则客户端一直转圈
    tags = _NEXT_TAGS.get(key)
    if tags is None:
        await ctx.answer_query("这条按钮已过期，请重新发送指令", show_alert=True)
        return

    try:
        pool = get_pool()
        if pool is None:
            await ctx.answer_query("图池尚未就绪", show_alert=True)
            return

        post = await pool.random_post(tags)
        if post is None:
            await ctx.answer_query("没有更多结果了", show_alert=True)
            return

        async with _get_lock():
            new_key = _remember_tags(tags)

        markup = build_keyboard(post.page_url, post.file_url, new_key)
        caption = cfg.get("caption").format(id=post.id)
        # 直接替换当前消息的媒体与按钮
        if _is_photo(post.file_url):
            await ctx.edit_photo(post.file_url, caption=caption, reply_markup=markup)
        else:
            await ctx.edit_document(
                post.file_url, caption=caption, filename=_filename(post), reply_markup=markup
            )
    except BotError as e:
        logger.warning("换一张失败: %s", e)
        await ctx.answer_query("发送失败，请重试", show_alert=True)
    except Exception:
        logger.exception("yiff_next 异常")
        await ctx.answer_query("出错了，请稍后再试", show_alert=True)


# --------------------------------------------------------------------------
# 取图通道开关：/fs on（FlareSolverr）| /fs off（直连）| /fs（看当前状态）
#
# 只改内存里的站点信息并重建客户端，图池不用重建 —— 它每次取图都 await fs()，
# 下一个请求自动走新通道。开关同时写回 config/yiff.json，重启后保持。
# --------------------------------------------------------------------------
_FS_ON = {"on", "1", "true", "yes", "y", "enable", "open", "fs"}
_FS_OFF = {"off", "0", "false", "no", "n", "disable", "close", "direct"}


async def _status_text() -> str:
    health = await fs_health()
    fs_mode = health.get("mode") == "flaresolverr"
    lines = [
        f"当前取图通道：{'FlareSolverr' if fs_mode else '直连'}",
        f"站点：{cfg.get('site')}",
    ]

    # 浏览器指纹：装了 curl_cffi 且填了 impersonate 才真的生效
    fp = health.get("impersonate") or ""
    if not fp:
        lines.append("浏览器指纹：未启用（pip install curl_cffi 后在 browser.impersonate 填 chrome124）")
    elif health.get("fingerprint_active"):
        lines.append(f"浏览器指纹：{fp}（已生效）")
    else:
        lines.append(f"浏览器指纹：{fp}（未生效，缺 curl_cffi）")
    if health.get("cookies"):
        lines.append(f"自定义 Cookie：{', '.join(health['cookies'])}")

    if fs_mode:
        if health.get("has_clearance"):
            lines.append(f"cf_clearance：有效（{health.get('expires_in')}s 后过期）")
        else:
            lines.append("cf_clearance：暂无，首次取图时会解挑战")

    # 实际发出的 UA：开了指纹会被换成浏览器 UA，这里显示的是替换之后的值
    browser = BrowserProfile.from_mapping(cfg.get("browser") or {})
    ua = health.get("user_agent") or browser.effective_ua(_fs_cfg().get("user_agent") or "")
    lines.append(f"UA：{ua or '（未配置，用默认）'}")
    return "\n".join(lines)


@listener("fs")
@listener("yiff_fs")
async def yiff_fs(ctx: Context, *args, **kwargs):
    user = ctx.user
    if user is None or not user.is_owner:
        return                                     # 非主人静默忽略

    arg = args[0].strip().lower() if args else ""
    if not arg or arg in ("status", "state"):
        await ctx.reply_text(await _status_text())
        return

    if arg in _FS_ON:
        target = True
    elif arg in _FS_OFF:
        target = False
    else:
        await ctx.reply_text("用法：/fs on（走 FlareSolverr）| /fs off（直连）| /fs（看状态）")
        return

    if target == _fs_enabled():
        await ctx.reply_text(f"已经是{'FlareSolverr' if target else '直连'}通道了")
        return

    _save_fs_enabled(target)
    await set_mode(target)                         # 丢旧客户端 + 按新通道重建
    if target:
        await ctx.reply_text(
            "已切到 FlareSolverr 通道，正在后台解挑战（首次取图可能稍慢）\n"
            "已写入 config/yiff.json"
        )
    else:
        await ctx.reply_text(
            "已切到直连通道，不再经过 FlareSolverr\n已写入 config/yiff.json"
        )


# --------------------------------------------------------------------------
# 图池生命周期：由本插件的配置驱动，挂在 core 的 startup / shutdown 钩子上。
# 池实例放在 utils.posts_pool 的模块级单例里 —— 插件模块会被 /reload 重新导入，
# 状态写在插件里热重载一次就没了。
# --------------------------------------------------------------------------
def _pool_settings() -> PoolSettings:
    pool_cfg = dict(cfg.get("pool") or {})
    pool_cfg.setdefault("site", cfg.get("site"))
    pool_cfg.setdefault("default_tags", cfg.get("default_tags"))
    return PoolSettings.from_mapping(pool_cfg)


@startup
async def _start_pool(application=None) -> None:
    await pool_start(_pool_settings())


@shutdown
async def _stop_pool(application=None) -> None:
    await pool_stop()
