"""浏览器指纹 + Cookie 配置：让 yiff 插件的访问"看起来像浏览器"。

为什么需要它
------------
httpx 只管发 HTTP 请求，它的 TLS 握手（JA3/JA4）与 HTTP/2 帧顺序是
**Python 客户端的形状**。Cloudflare 早在看 Cookie 之前就能凭这两项把请求
判成脚本，直接 403。

所以过 CF 需要两件事同时成立：
1. **指纹**：TLS / HTTP2 / 头顺序跟真实浏览器一致 —— 由 curl_cffi 提供
2. **Cookie**：站点 Cookie（登录态、手填的 cf_clearance…），自己从浏览器粘过来

本模块只负责"描述指纹和 Cookie 长什么样"以及"按描述造一个客户端"，
不含任何站点信息，站点相关的东西由 http_pool.SiteProfile 带进来。

浏览器指纹（curl_cffi）
----------------------
``pip install curl_cffi`` 后配置里填 ``impersonate`` 即可（chrome124 / firefox135 /
safari184 …），TLS 指纹、HTTP/2 帧、头顺序一整套跟着走。**没装 curl_cffi 时
自动退回 httpx**，行为与无指纹时完全一致，不会报错。

UA 一致性（关键）
----------------
开启指纹后，UA 必须换成对应浏览器的 UA，否则"指纹是 Chrome、UA 写着
MyTgBot/1.0"反而更可疑（CF 会拿 UA 与 TLS 指纹交叉验证）。所以
``sync_ua=True``（默认）时会把请求 UA 替换成指纹对应的浏览器 UA。

注意 e621/e926 这类站要求"UA 里带用户名、且不许用浏览器 UA"，与上面的
要求直接冲突。要用哪个你自己权衡：
- 站点没拦你、只是偶发拦截 -> 关掉 impersonate，保留合规 UA（sync_ua 就无所谓了）
- CF 已经把你挡在门外      -> 开 impersonate，牺牲站点的 UA 合规换通过率

配置示例（config/yiff.json 的 browser 段）::

    "browser": {
      "impersonate": "chrome124",          // 留空 = 不启用指纹（退回 httpx）
      "sync_ua": true,                     // 启用指纹时，用浏览器 UA 覆盖站点 UA
      "user_agent": "",                    // 手动指定 UA 时优先（自己保证与指纹一致）
      "cookies": {"cf_clearance": "xxx"},  // 或 "cookie": "a=1; b=2" 字符串二选一
      "cookie_domains": [],                // 额外要发 cookie 的域名（默认只有站点主域）
      "headers": {"Referer": "https://e621.net/"},
      "proxy": "",
      "timeout": 20.0,
      "verify": true
    }
"""

from __future__ import annotations

import inspect
import logging
import re
from dataclasses import dataclass, field
from http.cookiejar import CookieJar
from http.cookies import SimpleCookie
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

__all__ = [
    "BrowserProfile",
    "CURL_CFFI_AVAILABLE",
    "aclose_client",
    "create_client",
    "host_of",
    "impersonate_ua",
    "inject_cookies",
]


# ---------------------------------------------------------------- curl_cffi 可用性
try:  # pragma: no cover - 取决于运行环境装没装
    from curl_cffi.requests import AsyncSession as _CffiAsyncSession

    CURL_CFFI_AVAILABLE = True
except Exception:  # ImportError / OSError(缺 .so) 都算没装
    _CffiAsyncSession = None  # type: ignore[assignment]
    CURL_CFFI_AVAILABLE = False


# ---------------------------------------------------------------- UA 模板
# curl_cffi 的默认指纹是 macOS 平台，UA 也跟着用 macOS，避免"平台对不上"。
# 版本号从 impersonate 名字里解析（chrome124 -> 124.0.0.0），不用维护长表。
_UA_CHROME_MAC = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/{ver} Safari/537.36"
)
_UA_EDGE_WIN = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/{ver} Safari/537.36 Edg/{ver}"
)
_UA_FIREFOX_MAC = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:{major}.0) Gecko/20100101 Firefox/{major}.0"
_UA_SAFARI_MAC = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/{ver} Safari/605.1.15"
)
_UA_SAFARI_IOS = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS {os} like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/{ver} Mobile/15E148 Safari/604.1"
)
_UA_CHROME_ANDROID = (
    "Mozilla/5.0 (Linux; Android {android}; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/{ver} Mobile Safari/537.36"
)


def _resolve_alias(target: str) -> str:
    """把 'chrome' / 'safari_ios' 这类别名解析成具体版本（chrome150），便于查版本号。

    名字里已经带数字的（chrome124）原样返回，不去打扰 curl_cffi。
    """
    if not CURL_CFFI_AVAILABLE or re.search(r"\d", target):
        return target
    try:
        from curl_cffi.requests.impersonate import resolve_latest_browser_type

        return str(resolve_latest_browser_type(target))  # type: ignore[arg-type]
    except Exception:
        return target


def _safari_version(target: str) -> str:
    """safari17_2_ios -> 17.2；safari184 -> 18.4；safari153 -> 15.3；safari2601 -> 26.1。"""
    groups = re.findall(r"\d+", target)
    if not groups:
        return "17.0"
    if "_" in target and len(groups) >= 2:          # safari17_2(_ios)
        return f"{groups[0]}.{groups[1]}"
    digits = groups[0]                              # safari184 / safari153
    if len(digits) <= 2:
        return f"{digits}.0"
    if len(digits) == 3:
        return f"{digits[:2]}.{digits[2]}"
    return f"{digits[:2]}.{digits[2:].lstrip('0') or '0'}"


def impersonate_ua(target: str) -> Optional[str]:
    """按指纹名给出配套的浏览器 UA；认不出就返回 None（调用方回退到站点 UA）。

    认不出时**不猜**：UA 与 TLS 指纹对不上比不加指纹更容易被识别。
    """
    name = _resolve_alias((target or "").strip().lower())
    if not name:
        return None

    if "android" in name:
        groups = re.findall(r"\d+", name)
        major = groups[0] if groups else "131"
        ver = f"{major}.0.0.0"
        android = "14" if int(major) >= 130 else "12"     # 别让系统版本与 Chrome 版本差太远
        return _UA_CHROME_ANDROID.format(ver=ver, android=android)

    if name.startswith("safari"):
        ver = _safari_version(name)
        if "ios" in name:
            return _UA_SAFARI_IOS.format(ver=ver, os=ver.replace(".", "_"))
        return _UA_SAFARI_MAC.format(ver=ver)

    groups = re.findall(r"\d+", name)
    if not groups:
        return None
    major = groups[0]

    if name.startswith("firefox"):
        return _UA_FIREFOX_MAC.format(major=major)
    if name.startswith("edge"):
        ver = f"{major}.0.0.0"
        return _UA_EDGE_WIN.format(ver=ver)
    if name.startswith(("chrome", "tor")):
        return _UA_CHROME_MAC.format(ver=f"{major}.0.0.0")
    return None


# ---------------------------------------------------------------- Cookie 解析
def parse_cookie_header(text: str) -> dict[str, str]:
    """把 'a=1; b=2' 解析成 dict。带引号/非法段会跳过而不是炸掉。"""
    if not text:
        return {}
    try:
        jar = SimpleCookie()
        jar.load(str(text))
        return {k: v.value for k, v in jar.items()}
    except Exception as e:
        logger.warning("Cookie 字符串解析失败（忽略）: %s", e)
        return {}


def cookie_to_header(cookies: Mapping[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def host_of(url: str) -> str:
    """取主机名（去端口），用作 cookie 的 domain。"""
    return (urlparse(url or "").hostname or "").lstrip(".")


# ---------------------------------------------------------------- 配置对象
@dataclass(frozen=True)
class BrowserProfile:
    """描述"这一站要用什么浏览器身份、带哪些 Cookie"去访问。

    默认全空 = 不启用指纹、不带额外 Cookie，行为与改动前一致。
    """

    impersonate: str = ""                                   # chrome124 / firefox135 / safari184 ...
    sync_ua: bool = True                                    # 启用指纹时，用浏览器 UA 覆盖站点 UA
    user_agent: str = ""                                    # 手动指定 UA（优先于自动推导）
    cookies: Mapping[str, str] = field(default_factory=dict)  # 静态 Cookie（合并 cookies + cookie 字符串）
    cookie_domains: tuple[str, ...] = ()                    # 额外要发 Cookie 的域名
    headers: Mapping[str, str] = field(default_factory=dict)  # 附加请求头
    proxy: str = ""                                         # 留空则直连（不带代理）
    timeout: float = 20.0                                   # 单次请求超时（秒）
    verify: bool = True                                     # 自签证书场景可关（不建议）
    ja3: str = ""                                           # 进阶：自定义 JA3（通常不需要）
    akamai: str = ""                                        # 进阶：自定义 Akamai H2 指纹

    # ------------------------------------------------------------ 构造
    @classmethod
    def from_mapping(cls, data: Optional[Mapping[str, Any]]) -> "BrowserProfile":
        data = data or {}
        cookies: dict[str, str] = {}
        raw_cookies = data.get("cookies") or {}
        if isinstance(raw_cookies, Mapping):
            cookies.update({str(k): str(v) for k, v in raw_cookies.items()})
        # "cookie": "a=1; b=2" 与 dict 形式等价，字符串后加载，重名时覆盖 dict
        cookies.update(parse_cookie_header(str(data.get("cookie") or "")))

        domains = data.get("cookie_domains") or ()
        if isinstance(domains, str):
            domains = [d for d in re.split(r"[,\s]+", domains) if d]

        headers = data.get("headers") or {}
        return cls(
            impersonate=str(data.get("impersonate") or "").strip(),
            sync_ua=bool(data.get("sync_ua", True)),
            user_agent=str(data.get("user_agent") or "").strip(),
            cookies=cookies,
            cookie_domains=tuple(str(d).strip().lstrip(".") for d in domains if str(d).strip()),
            headers={str(k): str(v) for k, v in headers.items()} if isinstance(headers, Mapping) else {},
            proxy=str(data.get("proxy") or "").strip(),
            timeout=float(data.get("timeout") or 20.0),
            verify=bool(data.get("verify", True)),
            ja3=str(data.get("ja3") or "").strip(),
            akamai=str(data.get("akamai") or "").strip(),
        )

    # ------------------------------------------------------------ 查询
    @property
    def enabled(self) -> bool:
        """是否真的启用了指纹：填了 impersonate **且** curl_cffi 可用。

        用 ``_CffiAsyncSession is not None`` 判定而不是布尔开关，
        免得两者不一致时造出一个根本发不出去请求的客户端。
        """
        return bool(self.impersonate) and _CffiAsyncSession is not None

    @property
    def wants_fingerprint(self) -> bool:
        """用户想启用指纹（不管 curl_cffi 装没装），用于日志提示。"""
        return bool(self.impersonate)

    def impersonate_ua(self) -> Optional[str]:
        """指纹配套的 UA；没启用指纹或认不出返回 None。"""
        if not self.impersonate:
            return None
        return impersonate_ua(self.impersonate)

    def effective_ua(self, fallback: str = "") -> str:
        """最终请求用的 UA。

        优先级：手动 user_agent > 指纹 UA（sync_ua 且指纹真的生效）> 传入的站点 UA。

        只有在指纹**真的生效**时才替换：没装 curl_cffi 却把 UA 写成 Chrome，等于
        "UA 是浏览器、TLS 指纹是 Python"，比不换更容易被识破。
        """
        if self.user_agent:
            return self.user_agent
        if self.sync_ua and self.enabled:
            ua = self.impersonate_ua()
            if ua:
                return ua
        return fallback

    def domains_for(self, *urls: str) -> tuple[str, ...]:
        """Cookie 要绑定到哪些域名：站点主域 + 额外配置的域名。"""
        hosts = [host_of(u) for u in urls]
        hosts.extend(self.cookie_domains)
        seen: dict[str, None] = {}
        for h in hosts:
            if h:
                seen.setdefault(h, None)
        return tuple(seen)


# ---------------------------------------------------------------- 客户端工厂
def inject_cookies(client: Any, cookies: Mapping[str, str], domains: tuple[str, ...]) -> None:
    """把 cookie 写进客户端的 jar（httpx / curl_cffi 都支持 set）。

    必须走 jar：curl_cffi 会用 jar 覆盖手动传的 Cookie 头，直接塞 header 会丢。
    """
    if not cookies or client is None:
        return
    jar: Optional[CookieJar] = getattr(client, "cookies", None)
    if jar is None or not hasattr(jar, "set"):
        logger.warning("客户端没有 cookie jar，跳过 Cookie 注入")
        return
    for host in domains:
        for name, value in cookies.items():
            try:
                jar.set(name, value, domain=host, path="/")
            except Exception as e:    # 单个 cookie 写失败不影响其它
                logger.warning("写入 cookie %s 失败: %s", name, e)


def create_client(
    *,
    timeout: float = 20.0,
    proxy: Optional[str] = None,
    verify: bool = True,
    follow_redirects: bool = True,
    headers: Optional[Mapping[str, str]] = None,
    cookies: Optional[Mapping[str, str]] = None,
    cookie_domains: tuple[str, ...] = (),
    browser: Optional[BrowserProfile] = None,
    transport: Any = None,
) -> Any:
    """造一个异步 HTTP 客户端。

    - ``browser.impersonate`` 非空且装了 curl_cffi -> curl_cffi.AsyncSession（带指纹）
    - 否则                                         -> httpx.AsyncClient（原行为）
    - 传了 ``transport``（测试钩子）-> 强制 httpx，因为 curl_cffi 不支持注入 transport

    ``cookie_domains`` 非空时 Cookie 只发给这些域名（别把 cf_clearance 泄漏给
    CDN / 第三方域）；留空则发给所有域名。

    两种客户端都实现了 ``request/get/post/stream/cookies/headers``，
    调用方（图池、插件）不需要知道当前用的是哪个。
    """
    browser = browser or BrowserProfile()
    if transport is not None:
        # 测试注入的是 httpx transport，只能配 httpx 客户端
        import httpx

        client: Any = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0),
            proxy=proxy,
            follow_redirects=follow_redirects,
            verify=verify,
            headers=dict(headers or {}),
            transport=transport,
        )
        _seed_cookies(client, cookies, cookie_domains)
        return client

    if browser.enabled and _CffiAsyncSession is not None:
        kwargs: dict[str, Any] = {
            "impersonate": browser.impersonate,
            "timeout": timeout,
            "verify": verify,
            "proxy": proxy or browser.proxy or None,
            "headers": dict(headers or {}),
        }
        if browser.ja3:
            kwargs["ja3"] = browser.ja3
        if browser.akamai:
            kwargs["akamai"] = browser.akamai
        client = _CffiAsyncSession(**kwargs)  # type: ignore[operator]
        _seed_cookies(client, cookies, cookie_domains)
        return client

    if browser.wants_fingerprint:
        logger.warning(
            "配置了 browser.impersonate=%s 但没装 curl_cffi，已退回 httpx（无浏览器指纹）。"
            "执行 pip install curl_cffi 后重启即可生效",
            browser.impersonate,
        )

    import httpx

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=10.0),
        proxy=proxy or browser.proxy or None,
        follow_redirects=follow_redirects,
        verify=verify,
        headers=dict(headers or {}),
    )
    _seed_cookies(client, cookies, cookie_domains)
    return client


def _seed_cookies(
    client: Any, cookies: Optional[Mapping[str, str]], domains: tuple[str, ...]
) -> None:
    """预置 Cookie：给了域名就按域绑定，没给就全发（兜底）。"""
    if not cookies or client is None:
        return
    if domains:
        inject_cookies(client, cookies, domains)
        return
    jar = getattr(client, "cookies", None)
    if jar is None:
        return
    try:
        jar.update(dict(cookies))       # 不绑域：所有请求都带
    except Exception as e:
        logger.warning("预置 cookie 失败: %s", e)


async def aclose_client(client: Any) -> None:
    """统一关闭：httpx 是 aclose()，curl_cffi 是 close()。"""
    if client is None:
        return
    closer = getattr(client, "aclose", None) or getattr(client, "close", None)
    if closer is None:
        return
    result = closer()
    if inspect.isawaitable(result):
        await result
