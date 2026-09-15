"""Warframe Market 物品索引与「黑话」解析。

为什么需要它
------------
WM 的 API 只认 slug（``excalibur_prime_set``），而玩家说的是「满级充沛」「咖喱p」。
这一层负责把人话翻成 slug，分三步：

1. **官方名兜底**：物品清单里自带中英文官方名（v1 的 ``i18n`` / v2 的 ``slug``），
   直接建索引 —— 说官方名（含中文）天然就能命中，不需要维护任何词典。
2. **黑话词典**：``config/wm.json`` 的 ``aliases`` 段，键是玩家叫法，值是**物品基名**
   （不带 _prime / _set 等后缀）。只在官方名解释不了时才用。
3. **后缀规则**：``p`` / ``prime`` / ``圣装`` -> ``_prime``；
   ``套装`` / ``set`` -> ``_set``；``图纸`` / ``bp`` -> ``_blueprint``；
   机体/系统/神经光元/刀刃/枪管… 各自对应 part slug。
   组合出候选 slug，按优先级挑**真实存在**的那个。

模糊匹配不猜唯一解：命中多个就返回候选列表，交给用户在按钮里选 ——
把「充沛」错解成另一个物品比查不到更糟。
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional

from .wm_api import RANK_MAX

logger = logging.getLogger(__name__)

__all__ = [
    "Item",
    "ItemIndex",
    "ParsedQuery",
    "parse_query",
    "normalize",
]


# --------------------------------------------------------------------- 归一化
_PUNCT_RE = re.compile(r"[^\w\u4e00-\u9fff]+")


def normalize(text: str) -> str:
    """归一：全角转半角、小写、去掉空格与标点。'Excalibur Prime Set' -> 'excaliburprimeset'。"""
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = re.sub(r"[\s_\-]+", "", text)
    return _PUNCT_RE.sub("", text)


# --------------------------------------------------------------------- 数据模型
@dataclass(frozen=True)
class Item:
    slug: str
    name_en: str = ""
    name_zh: str = ""
    max_rank: int = 0
    thumb: str = ""
    tradable: bool = True
    tags: tuple[str, ...] = ()

    @property
    def display(self) -> str:
        if self.name_zh and self.name_zh != self.name_en:
            return f"{self.name_zh}（{self.name_en}）"
        return self.name_en or self.slug

    @property
    def has_rank(self) -> bool:
        """Mod / Arcane 这类有等级的，价格按 rank 分层。"""
        return self.max_rank > 0


# --------------------------------------------------------------------- 黑话规则
# part 关键词 -> slug 后缀
PART_WORDS: dict[str, str] = {
    "set": "set", "套装": "set", "一套": "set", "全套": "set", "整件": "set", "组": "set",
    "blueprint": "blueprint", "bp": "blueprint", "图纸": "blueprint", "蓝图": "blueprint", "设计图": "blueprint",
    "chassis": "chassis", "机体": "chassis", "机身": "chassis",
    "neuroptics": "neuroptics", "神经光元": "neuroptics", "神经": "neuroptics", "头部": "neuroptics", "头": "neuroptics",
    "systems": "systems", "系统": "systems", "系统组件": "systems",
    "harness": "harness", "束带": "harness",
    "blade": "blade", "刀刃": "blade", "刃": "blade",
    "hilt": "hilt", "握柄": "hilt", "柄": "hilt",
    "pouch": "pouch", "弹匣": "pouch", "弹药袋": "pouch",
    "barrel": "barrel", "枪管": "barrel", "炮管": "barrel",
    "receiver": "receiver", "枪机": "receiver", "机匣": "receiver",
    "stock": "stock", "枪托": "stock", "托": "stock",
    "link": "link", "链接": "link", "连接": "link",
    "disc": "disc", "刀盘": "disc", "盘": "disc",
    "handle": "handle", "手柄": "handle",
    "grip": "grip", "握把": "grip",
    "string": "string", "弓弦": "string", "弦": "string",
    "boot": "boot", "靴": "boot",
    "blades": "blades", "双刃": "blades",
    "guard": "guard", "护手": "guard",
    "heatsink": "heatsink", "散热片": "heatsink",
    "ornament": "ornament", "挂饰": "ornament",
    "stars": "stars", "星": "stars",
}

# 明确表示「Prime」的词
PRIME_WORDS = {"p", "prime", "pr", "圣装", "p甲", "prime甲", "圣"}
# 明确表示等级的 token
MAX_RANK_WORDS = {"满级", "满", "max", "maxrank", "满rank", "r满", "顶级", "满等"}
# 需要剥掉的噪声后缀词（"咖喱甲" 里的 "甲"）
NOISE_WORDS = {"甲", "战甲", "warframe", "wm", "的", "价格", "多少钱", "多少", "多少p", "求", "收", "出"}

_RANK_NUM_RE = re.compile(r"^(?:r|rank|lv|lvl|level)?(\d{1,2})$")
_RANK_CN_RE = re.compile(r"^(\d{1,2})级$")
# 等级标记可能是前后缀而不是独立词：满级充沛 / 充沛满级 / 充沛r3 / 充沛3级
_RANK_PREFIX_RE = re.compile(r"^(.+?)r(\d{1,2})$")
_RANK_SUFFIX_RE = re.compile(r"^(.+?)(\d{1,2})级$")


def _strip_rank(norm: str) -> Optional[tuple[Optional[int], str]]:
    """把 token 上挂着的等级标记拆出来，返回 (等级, 剩下部分)；没有则 None。

    等级用 RANK_MAX 表示「满级」，具体数字等拿到物品 max_rank 再代。
    """
    if not norm:
        return None
    # 长的先匹配，免得「满」抢在「满级」前面把名字切坏
    for word in sorted(MAX_RANK_WORDS, key=len, reverse=True):
        if norm == word:
            return (RANK_MAX, "")
        if len(norm) > len(word):
            if norm.startswith(word):
                return (RANK_MAX, norm[len(word):])
            if norm.endswith(word):
                return (RANK_MAX, norm[: -len(word)])
    matched = _RANK_PREFIX_RE.match(norm) or _RANK_SUFFIX_RE.match(norm)
    if matched:
        try:
            return (int(matched.group(2)), matched.group(1))
        except ValueError:
            return None
    matched = _RANK_CN_RE.match(norm) or _RANK_NUM_RE.match(norm)
    if matched:
        try:
            return (int(matched.group(1)), "")
        except ValueError:
            return None
    return None


@dataclass
class ParsedQuery:
    """解析结果。rank=RANK_MAX 表示「满级」，具体数字由物品的 max_rank 决定。"""

    raw: str = ""
    base: str = ""            # 归一化后的物品基名
    prime: bool = False
    part: Optional[str] = None
    rank: Optional[int] = None

    def describe(self) -> str:
        bits = []
        if self.prime:
            bits.append("Prime")
        if self.part:
            bits.append(self.part)
        if self.rank is not None:
            bits.append("满级" if self.rank == RANK_MAX else f"rank {self.rank}")
        return " · ".join(bits) or "无附加条件"


def parse_query(text: str) -> ParsedQuery:
    """把「满级充沛」「咖喱p 套装」「excalibur prime set r3」拆成结构化条件。"""
    parsed = ParsedQuery(raw=(text or "").strip())
    tokens = [t for t in re.split(r"[\s,，、]+", parsed.raw) if t]
    left: list[str] = []

    for token in tokens:
        low = token.strip().lower()
        if not low:
            continue
        norm = normalize(low)

        # 1) 等级（可能是独立词「满级」，也可能挂在名字上「满级充沛」）
        stripped = _strip_rank(norm)
        if stripped is not None:
            value, norm = stripped
            if value is not None and parsed.rank is None and (value == RANK_MAX or 0 <= value <= 20):
                parsed.rank = value
            if not norm:
                continue

        # 2) Prime
        if norm in PRIME_WORDS:
            parsed.prime = True
            continue

        # 3) 部位
        if norm in PART_WORDS:
            parsed.part = PART_WORDS[norm]
            continue

        # 4) 噪声词
        if norm in NOISE_WORDS:
            continue

        left.append(norm)      # 存归一化后的剩余部分，别把原 token 带上已剥离的等级

    base = normalize("".join(left))

    # 没显式写 prime 词，但名字自带：圣装咖喱 / 咖喱p / voltprime / 犀牛p甲
    if base.startswith("圣装"):
        base = base[2:]
        parsed.prime = True
    if base.endswith("甲") and len(base) > 1:      # 「咖喱甲」里的「甲」是噪声
        base = base[:-1]
    for marker in ("prime", "p"):
        if base.endswith(marker) and len(base) > len(marker):
            base = base[: -len(marker)]
            parsed.prime = True
            break

    parsed.base = base
    return parsed


# --------------------------------------------------------------------- 索引
def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _pick_names(raw: Mapping[str, Any]) -> tuple[str, str]:
    """从 v1/v2 的物品条目里取 (英文名, 中文名)，兼容 i18n 的两种形状。"""
    name_en = str(raw.get("item_name") or raw.get("name") or raw.get("en") or "")
    name_zh = ""
    i18n = raw.get("i18n")
    if isinstance(i18n, dict):                       # v2：{"zh": {...}} 或 {"zh": "..."}
        for lang in ("zh", "zh-hans", "zh-hant", "zh-hk"):
            value = i18n.get(lang)
            if isinstance(value, Mapping):
                name_zh = str(value.get("item_name") or value.get("name") or "")
            elif isinstance(value, str):
                name_zh = value
            if name_zh:
                break
    elif isinstance(i18n, list):                     # v1：[{"language":"zh","item_name":"..."}]
        for entry in i18n:
            if isinstance(entry, Mapping) and str(entry.get("language") or "").lower().startswith("zh"):
                name_zh = str(entry.get("item_name") or entry.get("name") or "")
                if name_zh:
                    break
    if not name_zh:
        name_zh = str(raw.get("zh") or raw.get("item_name_zh") or "")
    return name_en, name_zh


class ItemIndex:
    """slug / 官方名 / 黑话 三合一索引。"""

    def __init__(self) -> None:
        self._items: dict[str, Item] = {}
        self._keys: dict[str, list[str]] = {}     # 归一化名 -> slug 列表
        self._aliases: dict[str, str] = {}        # 归一化黑话 -> 物品基名
        self._loaded_at = 0.0

    # ---------------------------------------------------------- 构建
    def load(self, raw_items: Iterable[Mapping[str, Any]]) -> int:
        self._items.clear()
        self._keys.clear()
        for raw in raw_items:
            if not isinstance(raw, Mapping):
                continue
            slug = str(raw.get("url_name") or raw.get("slug") or "").strip()
            if not slug:
                continue
            name_en, name_zh = _pick_names(raw)
            max_rank = _as_int(
                raw.get("mod_max_rank", raw.get("maxRank", raw.get("max_rank"))), 0
            )
            tradable = raw.get("tradable")
            tags = raw.get("tags")
            item = Item(
                slug=slug,
                name_en=name_en or slug.replace("_", " ").title(),
                name_zh=name_zh,
                max_rank=max_rank,
                thumb=str(raw.get("thumb") or ""),
                tradable=bool(tradable) if tradable is not None else True,
                tags=tuple(str(t) for t in tags) if isinstance(tags, (list, tuple)) else (),
            )
            self._items[slug] = item
            for name in (slug, name_en, name_zh):
                key = normalize(name)
                if key:
                    self._keys.setdefault(key, [])
                    if slug not in self._keys[key]:
                        self._keys[key].append(slug)
        return len(self._items)

    def set_aliases(self, aliases: Optional[Mapping[str, Any]]) -> None:
        """黑话词典：{ "咖喱": "excalibur" }，值是不带后缀的物品基名。"""
        self._aliases.clear()
        for key, value in (aliases or {}).items():
            if str(key).startswith("_"):        # 跳过 _comment 之类的元信息
                continue
            norm = normalize(str(key))
            target = normalize(str(value))
            if norm and target:
                self._aliases[norm] = target

    # ---------------------------------------------------------- 查询
    def __len__(self) -> int:
        return len(self._items)

    def get(self, slug: str) -> Optional[Item]:
        return self._items.get(slug)

    def _candidates(self, base: str, prime: bool, part: Optional[str]) -> list[str]:
        """按优先级列出候选 slug（越靠前越可能）。"""
        if not base:
            return []
        if prime and part:
            order = [f"{base}_prime_{part}", f"{base}_{part}", f"{base}_prime", base]
        elif prime:
            order = [f"{base}_prime_set", f"{base}_prime", f"{base}_set", base]
        elif part:
            order = [f"{base}_{part}", f"{base}_prime_{part}", base]
        else:
            order = [f"{base}_set", base, f"{base}_prime_set", f"{base}_prime"]
        seen: list[str] = []
        for slug in order:
            if slug in self._items and slug not in seen:
                seen.append(slug)
        return seen

    def resolve(self, query: ParsedQuery, *, limit: int = 8) -> tuple[Optional[Item], list[Item]]:
        """返回 (唯一解, 候选列表)。

        唯一解为 None 且候选非空时，应该让用户自己选；
        两者都空才是真的没找到。
        """
        base = query.base
        if not base:
            return None, []

        # 1) 黑话词典
        alias_target = self._aliases.get(base)
        if alias_target:
            for slug in self._candidates(alias_target, query.prime, query.part):
                item = self._items.get(slug)
                if item is not None:
                    return item, []
            # 别名指向的名字没直接命中，拿它当关键词继续找
            base = alias_target

        # 2) 官方名（slug / 英文 / 中文）精确命中
        exact = self._keys.get(base) or []
        if exact:
            # 多个 slug 共享同一个显示名时（罕见），按 prime/part 再排一次序
            ranked = self._rank_by_shape(exact, query)
            if len(ranked) == 1:
                return ranked[0], []
            return None, ranked[:limit]

        # 3) 组合候选：base 自己就是基名（excalibur -> excalibur_prime_set）
        for slug in self._candidates(base, query.prime, query.part):
            item = self._items.get(slug)
            if item is not None:
                return item, []

        # 4) 模糊：前缀优先，其次子串
        prefix = [s for s in self._fuzzy(base) if normalize(s).startswith(base)]
        picked = prefix or self._fuzzy(base)
        items = [self._items[s] for s in picked[:limit] if s in self._items]
        return None, items

    def _rank_by_shape(self, slugs: list[str], query: ParsedQuery) -> list[Item]:
        """同名 slug 里，优先挑符合 prime/part 期望的那个。"""
        def score(slug: str) -> int:
            value = 0
            is_prime = "_prime" in slug
            if query.prime and is_prime:
                value -= 4
            if not query.prime and not is_prime:
                value -= 2
            if query.part and slug.endswith(f"_{query.part}"):
                value -= 3
            if not query.part and slug.endswith("_set"):
                value -= 1
            return value

        ordered = sorted(slugs, key=score)
        return [self._items[s] for s in ordered if s in self._items]

    def _fuzzy(self, base: str, *, cap: int = 40) -> list[str]:
        hits: list[tuple[int, str]] = []
        for key, slugs in self._keys.items():
            if base in key:
                hits.append((len(key), slugs[0]))
            if len(hits) >= cap * 4:
                break
        hits.sort(key=lambda pair: pair[0])
        out: list[str] = []
        for _, slug in hits:
            if slug not in out:
                out.append(slug)
            if len(out) >= cap:
                break
        return out

    def search(self, text: str, *, limit: int = 8) -> list[Item]:
        """纯关键字搜索（用户点候选/补名字时用）。"""
        key = normalize(text)
        if not key:
            return []
        exact = self._keys.get(key) or []
        found = [self._items[s] for s in exact if s in self._items]
        for slug in self._fuzzy(key, cap=limit * 3):
            item = self._items.get(slug)
            if item and item not in found:
                found.append(item)
            if len(found) >= limit:
                break
        return found[:limit]


# --------------------------------------------------------------------- 单例
_index = ItemIndex()


def get_index() -> ItemIndex:
    return _index


def reset_index() -> None:
    _index = ItemIndex()
    globals()["_index"] = _index
