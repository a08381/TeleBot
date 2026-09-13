"""plugins/yiff.py —— 发送带内联按钮的图片。

按钮分两类：
- 「原图链接 / 帖子页」是 URL 按钮，纯跳转，**不需要改动 main.py**
- 「换一张」是 callback 按钮，需要 main.py 注册一行 CallbackQueryHandler（见文件末尾）

发送策略（e621 的图可能是 webm/超大图，Telegram 对 photo 有格式和体积限制）：
1. 先试 reply_photo  → 能预览，体验最好
2. 失败则 reply_document → 支持任意格式
3. 仍失败则自己下载字节再发 → 绕开 Telegram 侧下载被 e926 拒绝的情况
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import PurePosixPath
from typing import Optional

from telegram import (
    Update,
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    InputMediaDocument,
    InputMediaPhoto,
)
from telegram.error import BadRequest, TelegramError

from plugin import listener, button
from posts_pool import get_pool

logger = logging.getLogger(__name__)

# ---------- 换一张：callback_data 有 64 字节上限，这里只存短 key ----------
# key -> tags，进程内缓存；重启即失效，无所谓
_NEXT_TAGS: dict[str, list[str]] = {}
_NEXT_LOCK: Optional[asyncio.Lock] = None
CB_PREFIX = "yiff:next:"

# Telegram 对 photo 的体积限制（通过 URL 发送时约 5MB）
PHOTO_URL_LIMIT = 5 * 1024 * 1024
# 自己下载后上传的体积上限（Bot API 普通上传 50MB，这里保守取 20MB）
UPLOAD_LIMIT = 20 * 1024 * 1024
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


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


def build_keyboard(page_url: str, file_url: str, next_key: Optional[str] = None,
                   enable_next: bool = True) -> InlineKeyboardMarkup:
    """按钮布局：第一行跳转类，第二行交互类。"""
    row1 = [
        InlineKeyboardButton("🔗 帖子页", url=page_url),
        InlineKeyboardButton("🖼 原图", url=file_url),
    ]
    rows = [row1]
    if enable_next and next_key:
        rows.append([InlineKeyboardButton("🎲 换一张", callback_data=f"{CB_PREFIX}{next_key}")])
    return InlineKeyboardMarkup(rows)


def _is_photo(url: str) -> bool:
    return PurePosixPath(url.split("?")[0]).suffix.lower() in PHOTO_EXTS


def _filename(post_id: int, url: str) -> str:
    ext = PurePosixPath(url.split("?")[0]).suffix or ".png"
    return f"{post_id}{ext}"


async def _send_media(bot: Bot, chat_id: int, post, markup: InlineKeyboardMarkup,
                      has_spoiler: bool = False) -> None:
    """photo -> document -> 自下载字节，三级回退。"""
    # 纯文本 caption：不设 parse_mode 时 Markdown 方括号会原样显示
    caption = f"帖子 {post.id}"

    # 1) photo（仅常见图片格式）
    if _is_photo(post.file_url):
        try:
            await bot.send_photo(
                chat_id=chat_id,
                photo=post.file_url,
                caption=caption,
                reply_markup=markup,
                has_spoiler=has_spoiler,
            )
            return
        except (BadRequest, TelegramError) as e:
            logger.info("send_photo 失败，回退 document: %s", e)

    # 2) document（URL 直传）
    try:
        await bot.send_document(
            chat_id=chat_id,
            document=post.file_url,
            caption=caption,
            reply_markup=markup,
            filename=_filename(post.id, post.file_url),
        )
        return
    except (BadRequest, TelegramError) as e:
        logger.info("send_document 失败，改为自己下载: %s", e)

    # 3) 自己下载字节上传（走带 cf_clearance 的单例，绕开 e926 对 Telegram 的拒绝）
    from fs_pool import fs
    client = await fs()
    resp = await client.get(post.file_url)
    resp.raise_for_status()
    data = resp.content
    if len(data) > UPLOAD_LIMIT:
        raise TelegramError(f"文件过大（{len(data) / 1024 / 1024:.1f}MB），跳过")
    await bot.send_document(
        chat_id=chat_id,
        document=InputFile(data, filename=_filename(post.id, post.file_url)),
        caption=caption,
        reply_markup=markup,
    )


@listener("yiff")
async def yiff(bot: Bot, update: Update, *args, **kwargs):
    tags = list(args)
    try:
        pool = await get_pool()
        post = await pool.random_post(tags)

        if post is None:
            await update.message.reply_text("未能找到所需图片")
            return

        async with _get_lock():
            next_key = _remember_tags(tags)

        markup = build_keyboard(post.page_url, post.file_url, next_key)
        await _send_media(bot, update.effective_chat.id, post, markup)

    except TelegramError as e:
        logger.warning("发送图片失败: %s", e)
        await update.message.reply_text(f"发送失败：{e}")
    except Exception:
        logger.exception("yiff 指令异常")
        await update.message.reply_text("出错了，请稍后再试")


# --------------------------------------------------------------------------
# 「换一张」回调处理（可选）
#
# 启用方式 —— main.py 里加两行：
#     from plugins.yiff import yiff_next
#     application.add_handler(CallbackQueryHandler(yiff_next, pattern=r"^yiff:next:"))
#
# 注意：reload_all_plugins() 会 pop 掉 plugins.* 模块，重新 import 后函数对象会变，
# 而 PTB 里注册的仍是旧函数。因此 add_handler 只在启动时执行一次即可；
# 若你希望热重载后回调也更新，需要在 reload 后重新 add_handler 并清掉旧 handler。
# --------------------------------------------------------------------------
@button("yiff:next:")
async def yiff_next(bot: Bot, update: Update, key: str) -> None:
    query = update.callback_query
    await query.answer()                       # 必须先应答，否则客户端一直转圈
    tags = _NEXT_TAGS.get(key)
    if tags is None:
        await query.answer("这条按钮已过期，请重新发送指令", show_alert=True)
        return

    try:
        pool = await get_pool()
        post = await pool.random_post(tags)
        if post is None:
            await query.answer("没有更多结果了", show_alert=True)
            return

        async with _get_lock():
            new_key = _remember_tags(tags)

        markup = build_keyboard(post.page_url, post.file_url, new_key)
        # 直接替换当前消息的媒体与按钮
        caption = f"帖子 {post.id}"
        media = (
            InputMediaPhoto(media=post.file_url, caption=caption)
            if _is_photo(post.file_url)
            else InputMediaDocument(
                media=post.file_url,
                caption=caption,
                filename=_filename(post.id, post.file_url),
            )
        )
        await query.edit_message_media(media=media, reply_markup=markup)
    except TelegramError as e:
        logger.warning("换一张失败: %s", e)
        await query.answer("发送失败，请重试", show_alert=True)
    except Exception:
        logger.exception("yiff_next 异常")
        await query.answer("出错了，请稍后再试", show_alert=True)
