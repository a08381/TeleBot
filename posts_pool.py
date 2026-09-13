"""
帖子预热池：后台提前把图拉好，指令来了直接取，用户侧零网络往返。

依赖 fs_pool 提供的 FlareSolverr 单例。挂在 post_init 里启动。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Optional
from urllib.parse import urlencode

from fs_pool import fs

logger = logging.getLogger(__name__)

SITE = "https://e926.net"
DEFAULT_TAGS = ["-intersex", "-female", "male", "order:rank"]

POOL_SIZE = 40
LOW_WATER = 10          # 池子低于此值就补货
REFILL_INTERVAL = 30.0  # 补货检查间隔
MIN_INTERVAL = 1.1      # e621 硬限 2 req/s，官方建议持续 <=1 req/s
CACHE_TTL = 300.0


@dataclass
class Post:
    id: int
    file_url: str
    page_url: str

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Optional["Post"]:
        try:
            return cls(
                id=int(raw["id"]),
                file_url=raw["file"]["url"],
                page_url=f"{SITE}/posts/{raw['id']}",
            )
        except (KeyError, TypeError, ValueError):
            return None


class PostPool:
    def __init__(self) -> None:
        self._pool: Deque[Post] = deque(maxlen=POOL_SIZE)
        self._cache: dict[str, tuple[float, list[Post]]] = {}
        self._throttle_lock = asyncio.Lock()
        self._last_at = 0.0
        self._task: Optional[asyncio.Task] = None
        self._stop: Optional[asyncio.Event] = None
        self._closed = False

    # ------------------------------------------------------------ 限流
    async def _throttle(self) -> None:
        async with self._throttle_lock:
            loop = asyncio.get_running_loop()
            delta = loop.time() - self._last_at
            if delta < MIN_INTERVAL:
                await asyncio.sleep(MIN_INTERVAL - delta)
            self._last_at = loop.time()

    # ------------------------------------------------------------ 拉取
    async def _fetch(self, tags: list[str], limit: int = 40,
                     page: Optional[int] = None) -> list[Post]:
        client = await fs()                     # 复用同一个 FlareSolverr 单例
        params: dict[str, Any] = {"tags": " ".join(tags), "limit": limit}
        if page:
            params["page"] = page
        url = f"{SITE}/posts.json?{urlencode(params)}"

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
        return [p for p in (Post.from_json(r) for r in payload.get("posts", [])) if p]

    async def search(self, tags: list[str]) -> list[Post]:
        """带缓存的搜索。"""
        key = " ".join(tags)
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and now - hit[0] < CACHE_TTL:
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
        posts = await self.search(tags or DEFAULT_TAGS)
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
            posts = await self._fetch(DEFAULT_TAGS, limit=40, page=random.randint(1, 30))
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
                await asyncio.wait_for(self._stop.wait(), timeout=REFILL_INTERVAL)
                return
            except asyncio.TimeoutError:
                pass
            if len(self._pool) < LOW_WATER:
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
        logger.info("帖子预热池已启动（当前 %d 张）", len(self._pool))

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
_pool: Optional[PostPool] = None
_pool_lock: Optional[asyncio.Lock] = None


def _get_lock() -> asyncio.Lock:
    global _pool_lock
    if _pool_lock is None:
        _pool_lock = asyncio.Lock()
    return _pool_lock


async def get_pool() -> PostPool:
    global _pool
    if _pool is not None:
        return _pool
    async with _get_lock():
        if _pool is None:
            _pool = PostPool()
    return _pool


async def pool_start(application: Any = None) -> None:
    p = await get_pool()
    await p.start()


async def pool_stop(application: Any = None) -> None:
    global _pool
    if _pool is not None:
        await _pool.stop()
        _pool = None
