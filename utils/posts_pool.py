"""帖子预热池：后台提前把图拉好，指令来了直接取，用户侧零网络往返。

这是一个**通用组件**，本身不含任何站点信息：
站点地址、标签、限流、池容量都由使用方（插件）通过 PoolSettings 注入，
依赖的 FlareSolverr 单例由 utils.fs_pool 提供。

以 yiff 插件为例：参数写在 config/yiff.json，插件在自己的 startup 钩子里
调用 pool_start(PoolSettings.from_mapping(...)) 启动本池。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Mapping, Optional
from urllib.parse import urlencode

from .fs_pool import fs

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PoolSettings:
    """图池参数。默认值适用于一般图站；e926/e621 的推荐值由插件配置给出。"""

    site: str = ""
    default_tags: tuple[str, ...] = ()
    size: int = 40                 # 预热池容量
    low_water: int = 10            # 低于此值就补货
    refill_interval: float = 30.0  # 补货检查间隔（秒）
    min_interval: float = 1.0      # 两次请求的最小间隔（站点限流）
    cache_ttl: float = 300.0

    @classmethod
    def from_mapping(cls, data: Optional[Mapping[str, Any]]) -> "PoolSettings":
        data = data or {}
        defaults = cls()
        tags = data.get("default_tags")
        return cls(
            site=str(data.get("site", defaults.site)).rstrip("/"),
            default_tags=tuple(tags) if tags else defaults.default_tags,
            size=int(data.get("size", defaults.size)),
            low_water=int(data.get("low_water", defaults.low_water)),
            refill_interval=float(data.get("refill_interval", defaults.refill_interval)),
            min_interval=float(data.get("min_interval", defaults.min_interval)),
            cache_ttl=float(data.get("cache_ttl", defaults.cache_ttl)),
        )


@dataclass
class Post:
    id: int
    file_url: str
    page_url: str

    @classmethod
    def from_json(cls, raw: dict[str, Any], site: str = "") -> Optional["Post"]:
        try:
            post_id = int(raw["id"])
            return cls(
                id=post_id,
                file_url=raw["file"]["url"],
                page_url=f"{site}/posts/{post_id}",
            )
        except (KeyError, TypeError, ValueError):
            return None


class PostPool:
    """把配置当参数收进来，自己不读任何配置文件。"""

    def __init__(self, cfg: PoolSettings) -> None:
        self._cfg = cfg
        self._pool: Deque[Post] = deque(maxlen=cfg.size)
        self._cache: dict[str, tuple[float, list[Post]]] = {}
        self._throttle_lock = asyncio.Lock()
        self._last_at = 0.0
        self._task: Optional[asyncio.Task] = None
        self._stop: Optional[asyncio.Event] = None
        self._closed = False

    # ------------------------------------------------------------ 配置
    @property
    def settings(self) -> PoolSettings:
        return self._cfg

    # ------------------------------------------------------------ 限流
    async def _throttle(self) -> None:
        async with self._throttle_lock:
            loop = asyncio.get_running_loop()
            delta = loop.time() - self._last_at
            if delta < self._cfg.min_interval:
                await asyncio.sleep(self._cfg.min_interval - delta)
            self._last_at = loop.time()

    # ------------------------------------------------------------ 拉取
    async def _fetch(self, tags: list[str], limit: int = 40,
                     page: Optional[int] = None) -> list[Post]:
        client = await fs()                     # 复用同一个 FlareSolverr 单例
        params: dict[str, Any] = {"tags": " ".join(tags), "limit": limit}
        if page:
            params["page"] = page
        url = f"{self._cfg.site}/posts.json?{urlencode(params)}"

        await self._throttle()
        resp = await client.get(url)            # 自动带 cf_clearance + 固定 UA
        if resp.status_code != 200:
            logger.warning("posts.json 返回 %s", resp.status_code)
            return []
        try:
            payload = resp.json()
        except Exception:
            logger.warning("posts.json 不是合法 JSON（可能又出挑战页）")
            return []
        return [
            p for p in (Post.from_json(r, self._cfg.site) for r in payload.get("posts", []))
            if p
        ]

    async def search(self, tags: list[str]) -> list[Post]:
        """带缓存的搜索。"""
        key = " ".join(tags)
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and now - hit[0] < self._cfg.cache_ttl:
            return hit[1]
        posts = await self._fetch(tags, limit=40, page=random.randint(1, 20))
        if not posts:
            posts = await self._fetch(tags, limit=20)     # 翻页越界，退回第一页
        self._cache[key] = (now, posts)
        return posts

    # ------------------------------------------------------------ 取图
    async def random_post(self, tags: list[str]) -> Optional[Post]:
        if not tags and self._pool:
            return self._pool.popleft()          # 命中预热池：零网络往返
        posts = await self.search(tags or list(self._cfg.default_tags))
        if not posts:
            return None
        chosen = random.choice(posts)
        if not tags:                             # 余量回填，下次直接命中
            seen = {p.id for p in self._pool}
            for p in posts:
                if p.id != chosen.id and p.id not in seen:
                    self._pool.append(p)
                    seen.add(p.id)
        return chosen

    # ------------------------------------------------------------ 补货
    async def _refill_once(self) -> int:
        try:
            posts = await self._fetch(
                list(self._cfg.default_tags), limit=40, page=random.randint(1, 30)
            )
        except Exception as e:
            logger.warning("预热补货失败: %s", e)
            return 0
        seen = {p.id for p in self._pool}
        added = 0
        for p in posts:
            if p.id not in seen:
                self._pool.append(p)
                seen.add(p.id)
                added += 1
        return added

    async def _loop(self) -> None:
        assert self._stop is not None
        while not self._closed:
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._cfg.refill_interval)
                return
            except asyncio.TimeoutError:
                pass
            if len(self._pool) < self._cfg.low_water:
                try:
                    await self._refill_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass

    async def start(self) -> None:
        if self._task is not None:
            return
        await self._refill_once()
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._loop())
        logger.info("帖子预热池已启动（%s，当前 %d 张）", self._cfg.site, len(self._pool))

    async def stop(self) -> None:
        self._closed = True
        if self._stop is not None:
            self._stop.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None

    @property
    def size(self) -> int:
        return len(self._pool)


# ---------------------------------------------------------------- 单例
# 池必须挂在模块级：plugins.* 会被 reload 清掉，实例放插件里热重载一次就没了
_pool: Optional[PostPool] = None
_started = False


def get_pool() -> Optional[PostPool]:
    """取当前图池；插件调用 start 之前为 None。"""
    return _pool


async def pool_start(cfg: PoolSettings, *, restart: bool = False) -> PostPool:
    """用给定配置创建并启动图池。

    :param cfg:      由插件配置构造的参数
    :param restart:  True 时先停掉旧池再按新配置重建（改了站点/标签时用）
    """
    global _pool, _started

    if _pool is not None and not restart:
        if _started:
            return _pool
        await _pool.start()
        _started = True
        return _pool

    if _pool is not None:
        await _pool.stop()

    _pool = PostPool(cfg)
    await _pool.start()
    _started = True
    return _pool


async def pool_stop() -> None:
    global _started
    if _pool is not None:
        await _pool.stop()
    _started = False
