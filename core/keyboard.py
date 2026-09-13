"""内联键盘构造器。

插件用它拼按钮，内部转成 InlineKeyboardMarkup，
所以 plugins/ 下不需要 import telegram。

用法::

    kb = (
        Keyboard()
        .url("🔗 帖子页", page_url)
        .url("🖼 原图", file_url)
        .row()
        .callback("🎲 换一张", f"{CB_PREFIX}{key}")
    )
    await ctx.reply_photo(url, reply_markup=kb)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


@dataclass(frozen=True)
class _Button:
    text: str
    url: Optional[str] = None
    callback_data: Optional[str] = None

    def as_telegram(self) -> InlineKeyboardButton:
        if self.url is not None:
            return InlineKeyboardButton(self.text, url=self.url)
        return InlineKeyboardButton(self.text, callback_data=self.callback_data or "")


class Keyboard:
    """按行累积按钮，row() 换行。"""

    __slots__ = ("_rows",)

    def __init__(self) -> None:
        self._rows: List[List[_Button]] = []

    # ------------------------------------------------------------ 构造
    def _push(self, button: _Button) -> "Keyboard":
        if not self._rows:
            self._rows.append([])
        self._rows[-1].append(button)
        return self

    def url(self, text: str, url: str) -> "Keyboard":
        """跳转按钮（纯 URL，不产生 callback）。"""
        return self._push(_Button(text, url=url))

    def callback(self, text: str, data: str) -> "Keyboard":
        """回调按钮，data 会走 button() 注册的分发。"""
        return self._push(_Button(text, callback_data=data))

    def row(self) -> "Keyboard":
        """结束当前行，后续按钮放到新的一行。"""
        if not self._rows or self._rows[-1]:
            self._rows.append([])
        return self

    # ------------------------------------------------------------ 产出
    def build(self) -> InlineKeyboardMarkup:
        rows = [[b.as_telegram() for b in row] for row in self._rows if row]
        return InlineKeyboardMarkup(rows)

    @property
    def rows(self) -> List[List[_Button]]:
        return [row for row in self._rows if row]

    def __len__(self) -> int:
        return sum(len(row) for row in self.rows)

    def __bool__(self) -> bool:
        return bool(len(self))

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"Keyboard(rows={len(self.rows)}, buttons={len(self)})"
