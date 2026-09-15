"""插件可见的上下文门面。

插件只能通过它与 Telegram 交互 —— 所有 telegram 类型都被封装成
这里定义的 User / Chat / Message / CallbackQuery / Keyboard，
因此 plugins/ 下不需要（也不应该）出现 `import telegram`。

插件函数签名::

    @listener("ping")
    async def ping(ctx: Context, *args, **kwargs):
        await ctx.reply_text("pong")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Union

from telegram import (
    Bot,
    InputFile,
    InputMediaDocument,
    InputMediaPhoto,
    Update,
)
from telegram.error import TelegramError

from utils.config import get_config

from .errors import BotError
from .keyboard import Keyboard

logger = logging.getLogger(__name__)

MediaSource = Union[str, bytes, Path]


async def _await(awaitable) -> Any:
    """统一把 SDK 异常翻译成 BotError。"""
    try:
        return await awaitable
    except TelegramError as exc:
        raise BotError(str(exc)) from exc


def _as_markup(markup: Optional[Keyboard]) -> Any:
    return markup.build() if isinstance(markup, Keyboard) else markup


def _as_media(value: MediaSource, filename: Optional[str] = None) -> Any:
    """bytes / Path 转成 InputFile，str（URL / file_id）原样透传。"""
    if isinstance(value, (bytes, bytearray)):
        return InputFile(bytes(value), filename=filename or "file")
    if isinstance(value, Path):
        return InputFile(value.open("rb"), filename=filename or value.name)
    return value


# --------------------------------------------------------------------- 数据门面
@dataclass(frozen=True)
class User:
    id: int
    username: Optional[str] = None
    full_name: str = ""
    is_bot: bool = False

    @property
    def is_owner(self) -> bool:
        """是否在配置的 owner_ids 里（管理指令用它做权限判断）。"""
        return self.id in get_config().owner_ids

    @property
    def mention(self) -> str:
        return f"@{self.username}" if self.username else self.full_name

    @classmethod
    def from_raw(cls, raw: Any) -> Optional["User"]:
        if raw is None:
            return None
        return cls(
            id=raw.id,
            username=getattr(raw, "username", None),
            full_name=getattr(raw, "full_name", "") or "",
            is_bot=bool(getattr(raw, "is_bot", False)),
        )


@dataclass(frozen=True)
class Chat:
    id: int
    type: str = ""
    title: Optional[str] = None

    @classmethod
    def from_raw(cls, raw: Any) -> Optional["Chat"]:
        if raw is None:
            return None
        return cls(id=raw.id, type=getattr(raw, "type", "") or "", title=getattr(raw, "title", None))


@dataclass(frozen=True)
class Message:
    id: int
    date: Optional[datetime] = None
    text: Optional[str] = None
    chat_id: Optional[int] = None

    @classmethod
    def from_raw(cls, raw: Any) -> Optional["Message"]:
        if raw is None:
            return None
        return cls(
            id=raw.message_id,
            date=getattr(raw, "date", None),
            text=getattr(raw, "text", None),
            chat_id=getattr(getattr(raw, "chat", None), "id", None),
        )


@dataclass
class CallbackQuery:
    """callback 按钮的门面；插件只用它读数据 + answer。"""

    id: str
    data: str = ""
    message: Optional[Message] = None
    _raw: Any = field(default=None, repr=False, compare=False)

    @classmethod
    def from_raw(cls, raw: Any) -> Optional["CallbackQuery"]:
        if raw is None:
            return None
        return cls(
            id=raw.id,
            data=raw.data or "",
            message=Message.from_raw(getattr(raw, "message", None)),
            _raw=raw,
        )

    async def answer(self, text: Optional[str] = None, *, show_alert: bool = False) -> None:
        """必须应答，否则客户端一直转圈。"""
        await _await(self._raw.answer(text=text, show_alert=show_alert))


# --------------------------------------------------------------------- 上下文
class Context:
    """一次更新（消息 / 回调）的上下文，插件唯一的操作入口。"""

    __slots__ = ("_bot", "_update")

    def __init__(self, bot: Bot, update: Update) -> None:
        self._bot = bot
        self._update = update

    # ------------------------------------------------------------ 逃生舱
    # 极少情况下插件需要原始对象（例如用 PTB 的高级特性），走这两个属性。
    @property
    def bot(self) -> Bot:
        return self._bot

    @property
    def update(self) -> Update:
        return self._update

    # ------------------------------------------------------------ 只读信息
    @property
    def user(self) -> Optional[User]:
        return User.from_raw(self._update.effective_user)

    @property
    def chat(self) -> Optional[Chat]:
        return Chat.from_raw(self._update.effective_chat)

    @property
    def chat_id(self) -> Optional[int]:
        chat = self._update.effective_chat
        return chat.id if chat else None

    @property
    def message(self) -> Optional[Message]:
        return Message.from_raw(self._update.effective_message)

    @property
    def query(self) -> Optional[CallbackQuery]:
        return CallbackQuery.from_raw(self._update.callback_query)

    @property
    def is_callback(self) -> bool:
        return self._update.callback_query is not None

    # ------------------------------------------------------------ 发送
    async def reply_text(self, text: str, **kwargs: Any):
        """回复当前消息所在的会话。"""
        message = self._update.effective_message
        if message is None:
            raise BotError("当前 update 没有可回复的消息")
        return await _await(
            message.reply_text(text, reply_markup=_as_markup(kwargs.pop("reply_markup", None)), **kwargs)
        )

    async def send_text(self, chat_id: Optional[int], text: str, **kwargs: Any):
        """向指定会话发文本；chat_id 为空时发到当前会话。"""
        target = chat_id if chat_id is not None else self.chat_id
        if target is None:
            raise BotError("无法确定目标会话")
        return await _await(
            self._bot.send_message(
                chat_id=target,
                text=text,
                reply_markup=_as_markup(kwargs.pop("reply_markup", None)),
                **kwargs,
            )
        )

    async def send_photo(
        self,
        photo: MediaSource,
        chat_id: Optional[int] = None,
        *,
        caption: Optional[str] = None,
        reply_markup: Optional[Keyboard] = None,
        filename: Optional[str] = None,
        **kwargs: Any,
    ):
        target = chat_id if chat_id is not None else self.chat_id
        if target is None:
            raise BotError("无法确定目标会话")
        return await _await(
            self._bot.send_photo(
                chat_id=target,
                photo=_as_media(photo, filename),
                caption=caption,
                reply_markup=_as_markup(reply_markup),
                **kwargs,
            )
        )

    async def reply_photo(
        self,
        photo: MediaSource,
        *,
        caption: Optional[str] = None,
        reply_markup: Optional[Keyboard] = None,
        filename: Optional[str] = None,
        **kwargs: Any,
    ):
        return await self.send_photo(
            photo, None, caption=caption, reply_markup=reply_markup, filename=filename, **kwargs
        )

    async def send_document(
        self,
        document: MediaSource,
        chat_id: Optional[int] = None,
        *,
        caption: Optional[str] = None,
        reply_markup: Optional[Keyboard] = None,
        filename: Optional[str] = None,
        **kwargs: Any,
    ):
        target = chat_id if chat_id is not None else self.chat_id
        if target is None:
            raise BotError("无法确定目标会话")
        return await _await(
            self._bot.send_document(
                chat_id=target,
                document=_as_media(document, filename),
                caption=caption,
                reply_markup=_as_markup(reply_markup),
                filename=filename,
                **kwargs,
            )
        )

    async def reply_document(
        self,
        document: MediaSource,
        *,
        caption: Optional[str] = None,
        reply_markup: Optional[Keyboard] = None,
        filename: Optional[str] = None,
        **kwargs: Any,
    ):
        return await self.send_document(
            document, None, caption=caption, reply_markup=reply_markup, filename=filename, **kwargs
        )

    # ------------------------------------------------------------ 编辑 / 应答
    def _edit_target(self):
        query = self._update.callback_query
        if query is not None and query.message is not None:
            return query.message
        return self._update.effective_message

    async def edit_photo(
        self,
        photo: MediaSource,
        *,
        caption: Optional[str] = None,
        reply_markup: Optional[Keyboard] = None,
        **kwargs: Any,
    ):
        return await self._edit_media(InputMediaPhoto(media=_as_media(photo), caption=caption, **kwargs), reply_markup)

    async def edit_document(
        self,
        document: MediaSource,
        *,
        caption: Optional[str] = None,
        reply_markup: Optional[Keyboard] = None,
        filename: Optional[str] = None,
        **kwargs: Any,
    ):
        media = InputMediaDocument(
            media=_as_media(document, filename), caption=caption, filename=filename, **kwargs
        )
        return await self._edit_media(media, reply_markup)

    async def _edit_media(self, media: Any, reply_markup: Optional[Keyboard]) -> None:
        target = self._edit_target()
        if target is None:
            raise BotError("当前 update 没有可编辑的消息")
        await _await(target.edit_media(media=media, reply_markup=_as_markup(reply_markup)))

    async def answer_query(self, text: Optional[str] = None, *, show_alert: bool = False) -> None:
        """应答 callback；当前 update 不是 callback 时静默跳过。"""
        query = self.query
        if query is not None:
            await query.answer(text, show_alert=show_alert)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"Context(chat_id={self.chat_id})"
