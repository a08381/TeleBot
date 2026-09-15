"""Warframe Market API 客户端（模块级单例，**多账号**）。

为什么单独一份，而不是复用 utils.http_pool
------------------------------------------
``http_pool`` 是**单站点**单例：它只维护一个 ``_current_site``，两个插件都调
``configure_site()`` 会互相抢占（源码里已经为此打了 warning）。yiff 拿它访问
e621，wm 要访问 api.warframe.market 且带 JWT —— 站点与身份都不同，
所以这里自建一份客户端。

放在 ``utils/`` 而不是插件里的原因同 README：``/reload`` 会重新导入
``plugins.*``，写在插件模块里的状态热重载一次就没了。

多账号模型
----------
**一个 Telegram user id 绑定一个 WM 账号**，会话全部存在 SessionStore 里
（``config/wm_sessions.json``，权限 0600）：

    WMClient（单例：连接池 + 限流器 + 缓存）
      └── SessionStore: {tg_user_id -> WMSession}

所以「谁在发指令」决定了用哪个账号 —— 所有需要登录的接口都要求显式传入
``session``，客户端自己**不持有**任何账号状态。这样 A 改状态不会动到 B 的账号，
也不会出现"单例里最后一个 token 覆盖所有人"的经典坑。

公开数据（物品清单、订单、统计）不需要账号，全局共享缓存，符合官方
"必须缓存响应"的要求。

API 现状（2026-09，合同版本仍 < 1.0，随时可能变）
------------------------------------------------
官方文档明确：v1 已弃用，但 OAuth 2.0 尚未开放，**需要用户授权的集成仍然要
走 v1 授权流程**。所以这里是「v1 拿凭证 + v2 读数据 + 状态写入双路径探测」：

    登录        POST  /v1/auth/signin      （社区强一致；官方说 v1 仍用于授权）
    当前用户    GET   /v2/me               （官方已文档化）
    订单        GET   /v2/orders/item/{slug}   （官方已文档化，公开无需登录）
    物品清单    GET   /v1/items            （回退 /v2/items）
    历史统计    GET   /v1/items/{slug}/statistics （社区一致，可选功能）
    状态写入    PATCH /v2/me  →  回退 PUT /v1/profile/status （官方未确认，故做成探测）

官方 Rules 要求：全局 **3 RPS**、User-Agent 必须可识别、必须缓存响应 ——
这里用令牌桶 + 清单/订单缓存来满足。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from .browser_profile import BrowserProfile, aclose_client, create_client

logger = logging.getLogger(__name__)

V1_BASE = "https://api.warframe.market/v1"
V2_BASE = "https://api.warframe.market/v2"
STATIC_BASE = "https://warframe.market/static/assets/"
ITEM_PAGE = "https://warframe.market/items/{slug}"

DEFAULT_UA = "TeleBot-WM/1.0 (+https://github.com/a08381/TeleBot)"

PLATFORMS = ("pc", "ps4", "xbox", "switch", "mobile")
STATUSES = ("invisible", "offline", "online", "ingame")
SETTABLE_STATUSES = ("invisible", "online", "ingame")
ONLINE_STATUSES = ("online", "ingame")

STATUS_LABEL = {
    "ingame": "🎮 游戏中（可交易）",
    "online": "🟢 在线（约 1 小时内可交易）",
    "invisible": "👻 隐身（不可交易）",
    "offline": "⚪️ 离线",
}

RANK_MAX = -1  # 「满级」哨兵值：具体数字要等拿到物品的 mod_max_rank 才知道


# --------------------------------------------------------------------- 异常
class WMError(Exception):
    """Warframe Market 相关错误。

    kind 决定插件怎么回话：
        auth     未绑定 / 凭证失效  -> 提示 /wmlogin
        rate     被限流             -> 提示稍后再试
        network  网络层失败
        config   本地配置问题（如状态写入被关掉）
        api      其它接口错误
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str = "api",
        status: Optional[int] = None,
        code: Optional[str] = None,
        detail: Any = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.code = code
        self.detail = detail


# --------------------------------------------------------------------- 限流
class _TokenBucket:
    """全局令牌桶。官方公共限流 3 RPS，超了 Cloudflare 会回 429 / 509。

    多账号共享同一个桶：限的是**这个 bot 进程**，不是单个账号。
    """

    def __init__(self, rate: float, burst: float) -> None:
        self._rate = max(0.1, float(rate or 3.0))
        self._burst = max(1.0, float(burst or self._rate))
        self._tokens = self._burst
        self._ts = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        wait = 0.0
        async with self._lock:
            now = time.monotonic()
            self._tokens = min(self._burst, self._tokens + (now - self._ts) * self._rate)
            self._ts = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
            else:
                wait = (1.0 - self._tokens) / self._rate
                self._tokens = 0.0
        if wait:
            await asyncio.sleep(wait)


# --------------------------------------------------------------------- 会话
@dataclass
class WMSession:
    """一个 Telegram 用户绑定的一个 WM 账号。"""

    tg_user_id: int = 0                 # 绑定的 Telegram user id（键）
    token: str = ""                     # JWT，敏感
    ingame_name: str = ""
    wm_user_id: str = ""
    status: str = ""
    platform: str = ""
    email: str = ""                     # 只用于回显，不用于自动重登
    bound_at: float = 0.0
    updated_at: float = 0.0

    @property
    def logged_in(self) -> bool:
        return bool(self.token)

    @property
    def label(self) -> str:
        """回显用：昵称优先，其次邮箱，最后 tg id。"""
        return self.ingame_name or self.email or f"#{self.tg_user_id}"

    def touch(self) -> None:
        self.updated_at = time.time()

    def as_stored(self) -> dict:
        data = asdict(self)
        data.pop("token", None)          # 落盘不含 token，见 SessionStore.save
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WMSession":
        try:
            tg_id = int(data.get("tg_user_id") or 0)
        except (TypeError, ValueError):
            tg_id = 0
        return cls(
            tg_user_id=tg_id,
            token=str(data.get("token") or ""),
            ingame_name=str(data.get("ingame_name") or ""),
            wm_user_id=str(data.get("wm_user_id") or data.get("user_id") or ""),
            status=str(data.get("status") or ""),
            platform=str(data.get("platform") or ""),
            email=str(data.get("email") or ""),
            bound_at=float(data.get("bound_at") or 0.0),
            updated_at=float(data.get("updated_at") or 0.0),
        )


class SessionStore:
    """{tg_user_id -> WMSession} 的落盘容器。

    键一律用 str(int) 存（JSON 只认字符串键），读回来转 int。
    整个文件权限 0600 —— 里面全是 JWT。
    """

    VERSION = 2

    def __init__(self, path: Optional[Any] = None) -> None:
        self.path = Path(path) if path else DEFAULT_SESSIONS_PATH
        self._sessions: dict[int, WMSession] = {}
        self.load()

    # ------------------------------------------------------------ 读写
    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("读取 WM 会话文件失败（忽略）: %s", exc)
            return
        if not isinstance(raw, dict):
            return
        if "sessions" not in raw and "token" in raw:
            # 旧版单账号文件：不知道该绑到谁身上，宁可让人重新登录也不猜
            logger.warning(
                "检测到旧版单账号会话文件 %s（无法自动迁移：不知道该绑到哪个用户），请重新 /wmlogin",
                self.path.name,
            )
            return
        for key, value in (raw.get("sessions") or {}).items():
            try:
                tg_id = int(key)
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict):
                session = WMSession.from_dict(value)
                session.tg_user_id = tg_id
                self._sessions[tg_id] = session

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "_comment": "Telegram user id -> WM 账号会话（含 JWT，勿外传）",
                "version": self.VERSION,
                "sessions": {str(k): asdict(v) for k, v in self._sessions.items()},
            }
            tmp = self.path.with_name(f"{self.path.name}.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)
            try:
                os.chmod(self.path, 0o600)      # 里面有 JWT
            except OSError:
                pass
        except Exception as exc:
            logger.warning("保存 WM 会话失败（忽略）: %s", exc)

    # ------------------------------------------------------------ 操作
    def get(self, tg_user_id: int) -> WMSession:
        """取会话；没绑过就返回一个**未入库**的空会话（logged_in=False）。"""
        try:
            key = int(tg_user_id)
        except (TypeError, ValueError):
            return WMSession()
        existing = self._sessions.get(key)
        if existing is not None:
            return existing
        return WMSession(tg_user_id=key)

    def put(self, session: WMSession) -> WMSession:
        if not session.tg_user_id:
            raise ValueError("会话缺少 tg_user_id，不能入库")
        if not session.bound_at:
            session.bound_at = time.time()
        session.touch()
        self._sessions[int(session.tg_user_id)] = session
        self.save()
        return session

    def remove(self, tg_user_id: int) -> bool:
        try:
            key = int(tg_user_id)
        except (TypeError, ValueError):
            return False
        existed = self._sessions.pop(key, None) is not None
        self.save()
        return existed

    def all(self) -> dict[int, WMSession]:
        return dict(self._sessions)

    def bound(self) -> list[WMSession]:
        """只返回真的登录过的（有 token）。"""
        return [s for s in self._sessions.values() if s.logged_in]

    def __len__(self) -> int:
        return len(self._sessions)


# --------------------------------------------------------------------- 解析
def _error_of(body: Any) -> Optional[str]:
    """从 v1/v2 的 error 段里取一句人话。"""
    if not isinstance(body, dict):
        return None
    err = body.get("error")
    if not err:
        return None
    if isinstance(err, str):
        return err
    if isinstance(err, dict):
        for key in ("code", "request", "message", "error"):
            value = err.get(key)
            if isinstance(value, (list, tuple)):
                return ", ".join(str(v) for v in value)
            if value:
                return str(value)
        return json.dumps(err, ensure_ascii=False)[:120]
    return str(err)


def unwrap_v2(body: Any) -> Any:
    """v2 信封：{"apiVersion":..., "data":..., "error":...}。"""
    err = _error_of(body)
    if err:
        raise WMError(f"接口返回错误：{err}", kind="api", code=err)
    if isinstance(body, dict) and "data" in body:
        return body.get("data")
    return None


def unwrap_v1(body: Any) -> Any:
    """v1：{"payload": {...}}。"""
    err = _error_of(body)
    if err:
        raise WMError(f"接口返回错误：{err}", kind="api", code=err)
    if isinstance(body, dict) and "payload" in body:
        return body.get("payload")
    return body


def _extract_orders(data: Any) -> list[dict]:
    """订单列表可能在 data、data["orders"]，或 data 的 sell/buy 两段里。"""
    if isinstance(data, list):
        return [o for o in data if isinstance(o, dict)]
    if not isinstance(data, dict):
        return []
    if isinstance(data.get("orders"), list):
        return [o for o in data["orders"] if isinstance(o, dict)]
    out: list[dict] = []
    for kind in ("sell", "buy"):
        for order in data.get(kind) or []:
            if isinstance(order, dict):
                merged = dict(order)
                merged.setdefault("type", kind)
                out.append(merged)
    return out


def normalize_order(raw: Mapping[str, Any]) -> dict:
    """把 v1 / v2 的订单字段抹平成统一形状。

    差异：type/order_type、rank/mod_rank、user.ingameName/ingame_name。
    """
    user = raw.get("user") or {}
    if not isinstance(user, dict):
        user = {}
    order_type = str(raw.get("type") or raw.get("order_type") or "").lower()

    rank = raw.get("rank")
    if rank is None:
        rank = raw.get("mod_rank")
    try:
        rank = int(rank) if rank is not None else 0
    except (TypeError, ValueError):
        rank = 0

    try:
        platinum = int(raw.get("platinum") or 0)
    except (TypeError, ValueError):
        platinum = 0
    try:
        quantity = int(raw.get("quantity") or 1)
    except (TypeError, ValueError):
        quantity = 1

    return {
        "id": str(raw.get("id") or ""),
        "type": order_type,
        "platinum": platinum,
        "quantity": quantity,
        "rank": rank,
        "visible": bool(raw.get("visible", True)),
        "user": str(user.get("ingameName") or user.get("ingame_name") or ""),
        "user_status": str(user.get("status") or ""),
        "platform": str(raw.get("platform") or ""),
    }


def thumb_url(thumb: str) -> str:
    """thumb 是相对路径（官方要求按静态资源基址拼），已是完整 URL 就原样用。"""
    if not thumb:
        return ""
    if thumb.startswith(("http://", "https://")):
        return thumb
    return STATIC_BASE + thumb.lstrip("/")


# --------------------------------------------------------------------- 客户端
class WMClient:
    """Warframe Market 客户端：限流 + 多账号会话 + 双版本回退。

    账号状态**不在**客户端里 —— 需要登录的接口都必须传 ``session``，
    session 来自 ``SessionStore.get(tg_user_id)``。
    """

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_UA,
        platform: str = "pc",
        language: str = "en",
        crossplay: bool = False,
        timeout: float = 20.0,
        browser: Any = None,
        session_path: Any = None,
        rate: float = 3.0,
        burst: float = 3.0,
        v1_base: str = V1_BASE,
        v2_base: str = V2_BASE,
        auth_scheme: str = "auto",
        items_ttl: float = 86400.0,
        orders_ttl: float = 60.0,
    ) -> None:
        self.user_agent = user_agent or DEFAULT_UA
        self.platform = platform if platform in PLATFORMS else "pc"
        self.language = language or "en"
        self.crossplay = bool(crossplay)
        self.timeout = float(timeout or 20.0)
        self.browser = browser if isinstance(browser, BrowserProfile) else BrowserProfile.from_mapping(browser or {})
        self.store = SessionStore(session_path)
        self.v1_base = (v1_base or V1_BASE).rstrip("/")
        self.v2_base = (v2_base or V2_BASE).rstrip("/")
        # auto：v1 用 JWT（历史写法），v2 用 Bearer（官方 v2 写法）
        self.auth_scheme = (auth_scheme or "auto").lower()

        self._limiter = _TokenBucket(rate, burst)
        self._client: Optional[Any] = None
        self._items_ttl = float(items_ttl or 86400.0)
        self._orders_ttl = float(orders_ttl or 60.0)
        self._items_cache: Optional[list[dict]] = None
        self._items_ts = 0.0
        self._orders_cache: dict[str, tuple[float, list[dict]]] = {}

    # ------------------------------------------------------------ 底层
    def _base_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": self.browser.effective_ua(self.user_agent),
            "Platform": self.platform,
            "Language": self.language,
            "Crossplay": "true" if self.crossplay else "false",
        }
        headers.update(self.browser.headers)
        return headers

    def _auth_value(self, url: str, token: str) -> str:
        scheme = self.auth_scheme
        if scheme == "auto":
            scheme = "jwt" if url.startswith(self.v1_base) else "bearer"
        return f"JWT {token}" if scheme == "jwt" else f"Bearer {token}"

    async def _client_obj(self) -> Any:
        if self._client is None:
            self._client = create_client(
                timeout=self.timeout,
                verify=self.browser.verify,
                follow_redirects=True,
                headers=self._base_headers(),
                cookies=dict(self.browser.cookies),
                cookie_domains=self.browser.domains_for(self.v1_base, self.v2_base),
                browser=self.browser,
            )
        return self._client

    async def _request(
        self,
        method: str,
        url: str,
        *,
        session: Optional[WMSession] = None,
        auth: bool = True,
        headers: Optional[Mapping[str, str]] = None,
        **kwargs: Any,
    ) -> Any:
        await self._limiter.acquire()
        client = await self._client_obj()
        merged = dict(headers or {})
        if auth:
            token = (session.token if session else "") or ""
            if not token:
                raise WMError("该用户还没有绑定 Warframe Market 账号，先 /wmlogin", kind="auth")
            merged["Authorization"] = self._auth_value(url, token)
        try:
            return await client.request(method, url, headers=merged, **kwargs)
        except Exception as exc:  # 网络层一律归到 network，插件不用猜
            raise WMError(f"请求失败：{exc}", kind="network") from exc

    @staticmethod
    def _json(resp: Any) -> Any:
        try:
            return resp.json()
        except Exception:
            return None

    def _guard(self, resp: Any, body: Any) -> None:
        status = int(getattr(resp, "status_code", 0) or 0)
        if status in (429, 509):
            retry = ""
            try:
                retry = resp.headers.get("Retry-After") or ""
            except Exception:
                pass
            raise WMError(
                f"被限流（HTTP {status}）" + (f"，建议 {retry}s 后重试" if retry else ""),
                kind="rate",
                status=status,
            )
        if status in (401, 403):
            raise WMError(
                "凭证无效或已过期，请重新 /wmlogin" if status == 401 else "没有权限（403）",
                kind="auth",
                status=status,
            )
        if status >= 400:
            detail = _error_of(body) or ""
            raise WMError(
                f"HTTP {status}" + (f"：{detail}" if detail else ""),
                kind="api",
                status=status,
                detail=detail,
            )
        if status == 0:
            raise WMError("没有拿到 HTTP 状态码", kind="network")

    async def call(
        self,
        method: str,
        url: str,
        *,
        session: Optional[WMSession] = None,
        auth: bool = True,
        v2: bool = True,
        headers: Optional[Mapping[str, str]] = None,
        **kwargs: Any,
    ) -> Any:
        """发请求并按 v1/v2 解开信封。

        ``auth=False`` 用于公开接口（清单 / 订单 / 统计），此时不需要 session；
        ``auth=True`` 而 session 没登录则抛 auth，由插件提示去 /wmlogin。
        """
        resp = await self._request(method, url, session=session, auth=auth, headers=headers, **kwargs)
        body = self._json(resp)
        self._guard(resp, body)
        data = unwrap_v2(body) if v2 else unwrap_v1(body)
        if data is None:
            raise WMError("响应格式不符（可能接口已变更）", kind="api", detail=str(body)[:200])
        return data

    # ------------------------------------------------------------ 会话查询
    def session_for(self, tg_user_id: Optional[int]) -> WMSession:
        """取某人的会话（没绑过返回空会话）。"""
        return self.store.get(tg_user_id or 0)

    def bound_accounts(self) -> list[WMSession]:
        return self.store.bound()

    # ------------------------------------------------------------ 登录
    @staticmethod
    def _extract_token(resp: Any, body: Any) -> str:
        """登录凭证优先取响应头 Authorization（JWT xxx / Bearer xxx），再退回 body。"""
        raw = ""
        try:
            raw = (resp.headers.get("authorization") or "").strip()
        except Exception:
            pass
        if raw:
            low = raw.lower()
            if low.startswith("jwt "):
                return raw[4:].strip()
            if low.startswith("bearer "):
                return raw[7:].strip()
            if " " not in raw and len(raw) > 20:
                return raw
        if isinstance(body, dict):
            payload = body.get("payload")
            for source in (body, payload if isinstance(payload, dict) else None, body.get("data")):
                if not isinstance(source, dict):
                    continue
                for key in ("jwt", "JWT", "token", "access_token"):
                    value = source.get(key)
                    if isinstance(value, str) and len(value) > 20:
                        return value.strip()
        return ""

    async def login(self, email: str, password: str, *, tg_user_id: int) -> WMSession:
        """给指定 Telegram 用户绑定 WM 账号。

        v1 signin（官方说 v1 仍用于授权），失败再试 v2。绑定成功后入库并落盘。
        同一 user id 重复登录会**覆盖**旧会话（一人一账号）。
        """
        email = (email or "").strip()
        if not email or not password:
            raise WMError("缺少邮箱或密码", kind="config")
        try:
            tg_user_id = int(tg_user_id)
        except (TypeError, ValueError):
            tg_user_id = 0
        if not tg_user_id:      # 0 不是合法 tg id，宁可报错也别绑到「没人」身上
            raise WMError("无法确定要绑定到哪个 Telegram 用户", kind="config")

        payload = {"email": email, "password": password, "auth_type": "header"}
        last: Optional[WMError] = None

        for base in (self.v1_base, self.v2_base):
            url = f"{base}/auth/signin"
            try:
                resp = await self._request(
                    "POST",
                    url,
                    auth=False,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": "JWT",  # v1 signin 的哨兵值，不是真 token
                    },
                    json=payload,
                )
            except WMError as exc:
                last = exc
                continue

            body = self._json(resp)
            status = int(getattr(resp, "status_code", 0) or 0)
            token = self._extract_token(resp, body)
            if status < 400 and token:
                session = WMSession(
                    tg_user_id=tg_user_id, token=token, email=email,
                    platform=self.platform, bound_at=time.time(),
                )
                # 顺手把昵称/状态填上；失败不影响绑定本身
                try:
                    me = await self.get_me(session)
                    self._apply_me(session, me)
                except WMError as exc:
                    logger.info("登录后拉取用户信息失败（不影响绑定）: %s", exc)
                return self.store.put(session)
            last = WMError(
                f"登录失败（HTTP {status}，未拿到凭证）", kind="auth", status=status,
                detail=_error_of(body) or "",
            )

        raise last or WMError("登录失败", kind="auth")

    def _apply_me(self, session: WMSession, me: Mapping[str, Any]) -> None:
        if not isinstance(session, WMSession) or not isinstance(me, dict):
            return
        name = me.get("ingameName") or me.get("ingame_name") or ""
        if name:
            session.ingame_name = str(name)
        uid = me.get("id") or me.get("user_id") or ""
        if uid:
            session.wm_user_id = str(uid)
        status = me.get("status") or ""
        if status:
            session.status = str(status)

    async def refresh(self, session: WMSession) -> bool:
        """试探 v1/auth/refresh：换到新 token 就 True，否则 False（需重新登录）。"""
        if not session or not session.logged_in:
            return False
        try:
            resp = await self._request("GET", f"{self.v1_base}/auth/refresh", session=session)
        except WMError:
            return False
        body = self._json(resp)
        token = self._extract_token(resp, body)
        if token:
            session.token = token
            session.touch()
            self.store.save()
            return True
        return False

    async def logout(self, session: WMSession) -> bool:
        """解绑（顺便通知 WM 端退出；失败也照样清本地）。"""
        if not session or not session.tg_user_id:
            return False
        if session.logged_in:
            try:
                await self._request("POST", f"{self.v1_base}/auth/signout", session=session)
            except WMError as exc:
                logger.info("signout 失败（本地会话照样清掉）: %s", exc)
        session.token = ""
        session.ingame_name = ""
        session.status = ""
        return self.store.remove(session.tg_user_id)

    # ------------------------------------------------------------ 用户信息
    async def get_me(self, session: Optional[WMSession] = None) -> dict:
        """GET /v2/me（官方已文档化）；失败回退 v1 /profile。结果会回写到 session。"""
        try:
            data = await self.call("GET", f"{self.v2_base}/me", session=session, v2=True)
        except WMError as exc:
            if exc.kind == "auth":
                raise
            last = exc
            try:
                data = await self.call("GET", f"{self.v1_base}/profile", session=session, v2=False)
            except WMError as exc2:
                raise WMError(f"读取用户信息失败：{last}；{exc2}", kind=exc2.kind) from exc2
            if isinstance(data, dict):
                inner = data.get("profile") or data.get("user")
                if isinstance(inner, dict):
                    data = inner
        if not isinstance(data, dict):
            raise WMError("用户信息格式不符", kind="api")
        if session is not None:
            self._apply_me(session, data)
            session.touch()
            if session.tg_user_id:
                self.store.put(session)
        return data

    # ------------------------------------------------------------ 在线状态
    async def set_status(
        self,
        status: str,
        session: Optional[WMSession] = None,
        *,
        mode: str = "auto",
        verify: bool = True,
    ) -> dict:
        """设置**某个账号**的在线状态。

        官方文档只确认了 profile 的 PATCH，**没有公开状态写入端点**，所以这里是
        「探测 + 回读校验」：先试 v2，再试 v1，写完都用 GET /v2/me 复核，
        复核不一致就如实告诉用户「请求已接受，但状态未确认」。
        """
        status = (status or "").strip().lower()
        if status not in SETTABLE_STATUSES:
            raise WMError(
                f"状态只能是 {' / '.join(SETTABLE_STATUSES)}（offline 是断开后的自动状态，不提供手动设置）",
                kind="config",
            )

        order = {"v2": ("v2",), "v1": ("v1",), "auto": ("v2", "v1"), "off": ()}.get(
            (mode or "auto").lower(), ("v2", "v1")
        )
        if not order:
            raise WMError("状态写入已关闭（status_write.mode = off）", kind="config")

        errors: list[str] = []
        used: Optional[str] = None
        for name in order:
            try:
                if name == "v2":
                    await self.call(
                        "PATCH", f"{self.v2_base}/me", session=session, v2=True,
                        headers={"Content-Type": "application/json"}, json={"status": status},
                    )
                else:
                    await self.call(
                        "PUT", f"{self.v1_base}/profile/status", session=session, v2=False,
                        headers={"Content-Type": "application/json"}, json={"status": status},
                    )
                used = name
                break
            except WMError as exc:
                errors.append(f"{name}: {exc}")
                if exc.kind == "auth":
                    raise

        if used is None:
            raise WMError("状态写入失败（" + "；".join(errors) + "）", kind="api")

        verified: Optional[str] = None
        if verify:
            try:
                me = await self.get_me(session)
                verified = str(me.get("status") or "") or None
            except WMError as exc:
                errors.append(f"回读校验失败: {exc}")

        return {
            "requested": status,
            "via": used,
            "verified": verified,
            "confirmed": verified is not None and verified.lower() == status,
            "errors": errors,
        }

    # ------------------------------------------------------------ 物品清单
    async def get_items(self, *, force: bool = False) -> list[dict]:
        """全量物品清单（用于名字 -> slug 解析）。官方要求缓存，默认 24h。

        公开数据，不分账号，全局共享缓存。
        """
        now = time.monotonic()
        if not force and self._items_cache is not None and (now - self._items_ts) < self._items_ttl:
            return self._items_cache

        raw_items: list[dict] = []
        errors: list[str] = []
        for base, v2 in ((self.v1_base, False), (self.v2_base, True)):
            try:
                data = await self.call("GET", f"{base}/items", session=None, auth=False, v2=v2)
            except WMError as exc:
                errors.append(f"{'v2' if v2 else 'v1'}/items: {exc}")
                continue
            if isinstance(data, dict):
                data = data.get("items") or data.get("data") or []
            if isinstance(data, list) and data:
                raw_items = [i for i in data if isinstance(i, dict)]
                break
        if not raw_items:
            raise WMError("没能拿到物品清单（" + "；".join(errors) + "）", kind="api")

        self._items_cache = raw_items
        self._items_ts = now
        return raw_items

    # ------------------------------------------------------------ 订单 / 统计
    async def get_orders(
        self,
        slug: str,
        *,
        platform: Optional[str] = None,
        crossplay: Optional[bool] = None,
        force: bool = False,
    ) -> list[dict]:
        """某物品的订单。v2 优先（官方已文档化），失败回退 v1。公开数据，无需登录。"""
        platform = platform or self.platform
        cross = self.crossplay if crossplay is None else bool(crossplay)
        key = f"{slug}|{platform}|{cross}"

        now = time.monotonic()
        if not force:
            cached = self._orders_cache.get(key)
            if cached and (now - cached[0]) < self._orders_ttl:
                return cached[1]

        params = {"platform": platform, "crossplay": str(cross).lower()}
        try:
            data = await self.call(
                "GET", f"{self.v2_base}/orders/item/{slug}", session=None, auth=False, v2=True, params=params
            )
            orders = _extract_orders(data)
        except WMError as exc_v2:
            try:
                data = await self.call(
                    "GET", f"{self.v1_base}/items/{slug}/orders", session=None, auth=False, v2=False, params=params
                )
                orders = _extract_orders(data)
            except WMError as exc_v1:
                raise WMError(f"查询订单失败：{exc_v2}；{exc_v1}", kind=exc_v1.kind) from exc_v1

        normalized = [normalize_order(o) for o in orders]
        self._orders_cache[key] = (now, normalized)
        return normalized

    async def get_statistics(self, slug: str) -> dict:
        """v1 历史统计（48 小时 / 90 天）。官方 v2 没给等价合同，属于可选功能。"""
        data = await self.call(
            "GET", f"{self.v1_base}/items/{slug}/statistics", session=None, auth=False, v2=False,
            params={"language": self.language},
        )
        closed = {}
        if isinstance(data, dict):
            closed = data.get("statistics_closed") or {}
        if not isinstance(closed, dict):
            closed = {}
        out: dict[str, Any] = {}
        for bucket in ("48hours", "90days"):
            series = closed.get(bucket)
            if isinstance(series, list) and series:
                latest = series[-1]
                if isinstance(latest, dict):
                    out[bucket] = {
                        "median": latest.get("median"),
                        "avg": latest.get("avg_price"),
                        "min": latest.get("min_price"),
                        "max": latest.get("max_price"),
                        "volume": latest.get("volume"),
                        "mod_rank": latest.get("mod_rank"),
                        "datetime": latest.get("datetime") or "",
                    }
        return out

    # ------------------------------------------------------------ 诊断
    async def probe(self, session: Optional[WMSession] = None, slug: str = "nikana_prime_set") -> list[tuple[str, bool, str]]:
        """逐个试探端点，返回 [(名称, 是否可用, 说明)]。给 /wmdiag 用。"""
        checks: list[tuple[str, bool, str]] = []

        async def run(name: str, coro) -> None:
            try:
                data = await coro
                size = len(data) if isinstance(data, (list, dict)) else "-"
                checks.append((name, True, f"OK（{size}）"))
            except WMError as exc:
                checks.append((name, False, f"{exc}"))
            except Exception as exc:  # 兜底，别让诊断自己炸掉
                checks.append((name, False, f"{type(exc).__name__}: {exc}"))

        await run("v1/items（公开）", self.get_items(force=True))
        await run(f"v2/orders/item/{slug}（公开）", self.get_orders(slug, force=True))
        await run(f"v1/items/{slug}/statistics（公开）", self.get_statistics(slug))
        if session is not None and session.logged_in:
            await run("v2/me（需登录）", self.get_me(session))
            await run("PATCH v2/me（状态写入）", self.set_status("online", session, mode="v2"))
            await run("PUT v1/profile/status（状态写入）", self.set_status("online", session, mode="v1"))
        else:
            checks.append(("v2/me（需登录）", False, "当前用户未绑定账号，跳过"))
        return checks

    # ------------------------------------------------------------ 生命周期
    async def aclose(self) -> None:
        self.store.save()
        client, self._client = self._client, None
        await aclose_client(client)


# --------------------------------------------------------------------- 单例
BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_SESSIONS_PATH = BASE_DIR / "config" / "wm_sessions.json"

_desired: Optional[WMClient] = None
_desired_sig = ""
_client: Optional[WMClient] = None
_client_sig = ""


def _signature(settings: Mapping[str, Any]) -> str:
    try:
        return json.dumps(settings, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        return str(time.monotonic())


def configure(settings: Mapping[str, Any]) -> WMClient:
    """由插件在 import 时登记「期望的客户端参数」（同步，不建连接）。"""
    global _desired, _desired_sig
    data = dict(settings or {})
    _desired = WMClient(
        user_agent=data.get("user_agent") or DEFAULT_UA,
        platform=data.get("platform") or "pc",
        language=data.get("language") or "en",
        crossplay=bool(data.get("crossplay")),
        timeout=float((data.get("api") or {}).get("timeout") or 20.0),
        browser=data.get("browser"),
        session_path=data.get("session_file") or DEFAULT_SESSIONS_PATH,
        rate=float((data.get("rate_limit") or {}).get("rps") or 3.0),
        burst=float((data.get("rate_limit") or {}).get("burst") or 3.0),
        v1_base=(data.get("api") or {}).get("v1_base") or V1_BASE,
        v2_base=(data.get("api") or {}).get("v2_base") or V2_BASE,
        auth_scheme=(data.get("api") or {}).get("auth_scheme") or "auto",
        items_ttl=float((data.get("cache") or {}).get("items_ttl") or 86400.0),
        orders_ttl=float((data.get("cache") or {}).get("orders_ttl") or 60.0),
    )
    _desired_sig = _signature(data)
    return _desired


async def wm() -> WMClient:
    """拿当前客户端；配置变了就换掉旧的（异步上下文里关闭，安全）。"""
    global _client, _client_sig
    if _desired is None:
        if _client is None:
            _client = WMClient()
            _client_sig = ""
        return _client
    if _client is not None and _client_sig == _desired_sig:
        return _client
    if _client is not None:
        try:
            await _client.aclose()
        except Exception as exc:
            logger.warning("关闭旧 WM 客户端失败: %s", exc)
    _client = _desired
    _client_sig = _desired_sig
    return _client


def current() -> Optional[WMClient]:
    return _client


async def wm_start(application: Any = None) -> None:
    """挂到 startup：建客户端（具体账号由插件按需绑定 / 自动登录）。"""
    client = await wm()
    logger.info(
        "WM 客户端就绪（平台=%s，跨屏=%s，已绑定 %d 个账号）",
        client.platform, client.crossplay, len(client.store.bound()),
    )


async def wm_stop(application: Any = None) -> None:
    global _client, _client_sig
    if _client is not None:
        try:
            await _client.aclose()
        except Exception as exc:
            logger.warning("关闭 WM 客户端失败: %s", exc)
        _client, _client_sig = None, ""
