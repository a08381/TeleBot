"""Microsoft Graph OAuth and E5 renewal support.

The store keeps one OAuth client per Telegram account. Refresh tokens and client
secrets are sensitive; the SQLite database is created with owner-only permissions
where the operating system supports them.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Generator, Mapping, Optional
from urllib.parse import parse_qs, urlencode, urlsplit

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parents[1]
AUTHORITY = "https://login.microsoftonline.com/common/oauth2/v2.0"
GRAPH_API = "https://graph.microsoft.com/v1.0"
DEFAULT_SCOPE = "openid offline_access User.Read Mail.Read"
E5_CALLBACK_PATH = "/e5/callback"


class E5RenewalError(Exception):
    """Expected OAuth or Microsoft Graph failure with a user-safe message."""


def _httpx_module():
    try:
        import httpx
    except ImportError as exc:
        raise E5RenewalError(
            "运行 E5 续订需要安装 requirements.txt 中的 httpx 依赖"
        ) from exc
    return httpx


@dataclass(frozen=True)
class OAuthCallback:
    code: str
    state: str
    error: str = ""


@dataclass(frozen=True)
class E5Account:
    id: int
    tg_user_id: int
    alias: str
    client_id: str
    client_secret: str = field(repr=False)
    refresh_token: str = field(repr=False)
    ms_user_id: str = ""
    user_principal_name: str = ""
    display_name: str = ""
    last_success_at: str = ""
    error_count: int = 0
    last_error: str = ""
    enabled: bool = True


@dataclass(frozen=True)
class RenewalResult:
    account: E5Account
    success: bool
    error: str = ""
    disabled: bool = False


def build_authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    """Build the delegated Microsoft identity authorization URL."""
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": DEFAULT_SCOPE,
        "state": state,
    }
    return f"{AUTHORITY}/authorize?{urlencode(params)}"


def build_webhook_callback_uri(public_url: str, callback_path: str = E5_CALLBACK_PATH) -> str:
    """Build the public HTTPS callback URI served by the shared webhook listener."""
    base = urlsplit(public_url.strip())
    path = "/" + callback_path.strip("/")
    if (
        base.scheme.lower() != "https"
        or not base.netloc
        or base.username
        or base.password
        or base.query
        or base.fragment
    ):
        raise E5RenewalError("Webhook 公网地址必须是有效的 HTTPS URL")
    if path == "/" or base.path not in ("", "/"):
        raise E5RenewalError("Webhook 公网地址和 OAuth 回调路径配置无效")
    return f"{base.scheme}://{base.netloc}{path}"


def parse_authorization_callback(value: str, redirect_uri: str) -> OAuthCallback:
    """Validate a pasted localhost callback and extract its code and state."""
    callback = urlsplit(value.strip())
    expected = urlsplit(redirect_uri)
    callback_path = callback.path.rstrip("/") or "/"
    expected_path = expected.path.rstrip("/") or "/"

    if (
        callback.scheme.lower() != expected.scheme.lower()
        or callback.netloc.lower() != expected.netloc.lower()
        or callback_path != expected_path
    ):
        raise E5RenewalError("回调地址与配置的 redirect_uri 不匹配")

    params = parse_qs(callback.query, keep_blank_values=True)
    state = (params.get("state") or [""])[0]
    error = (params.get("error_description") or params.get("error") or [""])[0]
    code = (params.get("code") or [""])[0]
    if not state:
        raise E5RenewalError("回调地址缺少 OAuth state，请重新执行 /e5bind")
    if not code and not error:
        raise E5RenewalError("回调地址缺少 authorization code")
    return OAuthCallback(code=code, state=state, error=error[:300])


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class E5RenewalStore:
    """Small SQLite repository for account credentials and in-progress binds."""

    def __init__(self, path: str | Path) -> None:
        target = Path(path).expanduser()
        self.path = target if target.is_absolute() else BASE_DIR / target
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS e5_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tg_user_id INTEGER NOT NULL,
                    alias TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    client_secret TEXT NOT NULL,
                    refresh_token TEXT NOT NULL,
                    ms_user_id TEXT NOT NULL DEFAULT '',
                    user_principal_name TEXT NOT NULL DEFAULT '',
                    display_name TEXT NOT NULL DEFAULT '',
                    last_success_at TEXT NOT NULL DEFAULT '',
                    error_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(tg_user_id, client_id)
                );
                CREATE INDEX IF NOT EXISTS idx_e5_accounts_enabled
                    ON e5_accounts(enabled);
                CREATE TABLE IF NOT EXISTS e5_pending_binds (
                    tg_user_id INTEGER PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    client_secret TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
        self._restrict_permissions()

    @contextmanager
    def _connection(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _restrict_permissions(self) -> None:
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            logger.warning("无法限制 E5 凭据数据库文件权限: %s", self.path)

    @staticmethod
    def _account(row: sqlite3.Row) -> E5Account:
        return E5Account(
            id=int(row["id"]),
            tg_user_id=int(row["tg_user_id"]),
            alias=str(row["alias"]),
            client_id=str(row["client_id"]),
            client_secret=str(row["client_secret"]),
            refresh_token=str(row["refresh_token"]),
            ms_user_id=str(row["ms_user_id"]),
            user_principal_name=str(row["user_principal_name"]),
            display_name=str(row["display_name"]),
            last_success_at=str(row["last_success_at"]),
            error_count=int(row["error_count"]),
            last_error=str(row["last_error"]),
            enabled=bool(row["enabled"]),
        )

    def save_pending(
        self, tg_user_id: int, client_id: str, client_secret: str, state: str
    ) -> None:
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                INSERT INTO e5_pending_binds
                    (tg_user_id, client_id, client_secret, state, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(tg_user_id) DO UPDATE SET
                    client_id=excluded.client_id,
                    client_secret=excluded.client_secret,
                    state=excluded.state,
                    created_at=excluded.created_at
                """,
                (tg_user_id, client_id, client_secret, state, _utc_now()),
            )

    def get_pending(self, tg_user_id: int) -> Optional[dict[str, str]]:
        with self._lock, self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM e5_pending_binds WHERE tg_user_id = ?", (tg_user_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_pending_by_state(self, state: str) -> Optional[dict[str, str]]:
        with self._lock, self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM e5_pending_binds WHERE state = ?", (state,)
            ).fetchone()
        return dict(row) if row else None

    def delete_pending(self, tg_user_id: int) -> None:
        with self._lock, self._connection() as conn:
            conn.execute("DELETE FROM e5_pending_binds WHERE tg_user_id = ?", (tg_user_id,))

    def delete_pending_by_state(self, state: str) -> None:
        with self._lock, self._connection() as conn:
            conn.execute("DELETE FROM e5_pending_binds WHERE state = ?", (state,))

    def count_for_user(self, tg_user_id: int) -> int:
        with self._lock, self._connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM e5_accounts WHERE tg_user_id = ?",
                (tg_user_id,),
            ).fetchone()
        return int(row["count"])

    def add_account(
        self,
        *,
        tg_user_id: int,
        alias: str,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        profile: Mapping[str, Any],
    ) -> E5Account:
        now = _utc_now()
        try:
            with self._lock, self._connection() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO e5_accounts (
                        tg_user_id, alias, client_id, client_secret, refresh_token,
                        ms_user_id, user_principal_name, display_name, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tg_user_id,
                        alias,
                        client_id,
                        client_secret,
                        refresh_token,
                        str(profile.get("id") or ""),
                        str(profile.get("userPrincipalName") or ""),
                        str(profile.get("displayName") or ""),
                        now,
                        now,
                    ),
                )
                account_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ValueError("该 Microsoft 应用已绑定") from exc
        account = self.get_owned_account(account_id, tg_user_id)
        if account is None:
            raise RuntimeError("新建的 E5 账户无法读取")
        return account

    def list_for_user(self, tg_user_id: int) -> list[E5Account]:
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM e5_accounts WHERE tg_user_id = ? ORDER BY id",
                (tg_user_id,),
            ).fetchall()
        return [self._account(row) for row in rows]

    def list_enabled(self) -> list[E5Account]:
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM e5_accounts WHERE enabled = 1 ORDER BY id"
            ).fetchall()
        return [self._account(row) for row in rows]

    def get_owned_account(self, account_id: int, tg_user_id: int) -> Optional[E5Account]:
        with self._lock, self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM e5_accounts WHERE id = ? AND tg_user_id = ?",
                (account_id, tg_user_id),
            ).fetchone()
        return self._account(row) if row else None

    def delete_owned_account(self, account_id: int, tg_user_id: int) -> bool:
        with self._lock, self._connection() as conn:
            cursor = conn.execute(
                "DELETE FROM e5_accounts WHERE id = ? AND tg_user_id = ?",
                (account_id, tg_user_id),
            )
        return cursor.rowcount > 0

    def record_success(
        self, account_id: int, refresh_token: str, profile: Mapping[str, Any]
    ) -> None:
        now = _utc_now()
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                UPDATE e5_accounts SET
                    refresh_token = ?,
                    ms_user_id = ?,
                    user_principal_name = ?,
                    display_name = ?,
                    last_success_at = ?,
                    error_count = 0,
                    last_error = '',
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    refresh_token,
                    str(profile.get("id") or ""),
                    str(profile.get("userPrincipalName") or ""),
                    str(profile.get("displayName") or ""),
                    now,
                    now,
                    account_id,
                ),
            )

    def record_failure(
        self, account_id: int, error: str, max_consecutive_errors: int
    ) -> tuple[int, bool]:
        with self._lock, self._connection() as conn:
            row = conn.execute(
                "SELECT error_count FROM e5_accounts WHERE id = ?", (account_id,)
            ).fetchone()
            if row is None:
                raise LookupError(f"E5 account {account_id} disappeared during renewal")
            error_count = int(row["error_count"]) + 1
            disabled = max_consecutive_errors > 0 and error_count >= max_consecutive_errors
            conn.execute(
                """
                UPDATE e5_accounts SET error_count = ?, last_error = ?,
                    enabled = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    error_count,
                    error.strip()[:300],
                    0 if disabled else 1,
                    _utc_now(),
                    account_id,
                ),
            )
        return error_count, disabled


class E5RenewalService:
    """OAuth binding, delegated Graph calls and the periodic renewal loop."""

    def __init__(self, settings: Mapping[str, Any]) -> None:
        self._settings = dict(settings)
        self.store = E5RenewalStore(self._settings["database_path"])
        self._task: Optional[asyncio.Task] = None
        self._run_lock = asyncio.Lock()

    def configure(self, settings: Mapping[str, Any]) -> None:
        self._settings = dict(settings)
        new_path = Path(str(settings["database_path"])).expanduser()
        if not new_path.is_absolute():
            new_path = BASE_DIR / new_path
        if new_path != self.store.path:
            self.store = E5RenewalStore(new_path)

    @property
    def redirect_uri(self) -> str:
        return str(self._settings["redirect_uri"]).strip()

    def authorize_url(self, client_id: str, state: str) -> str:
        return build_authorize_url(client_id, self.redirect_uri, state)

    async def exchange_code(
        self, client_id: str, client_secret: str, code: str
    ) -> tuple[str, dict[str, Any]]:
        httpx = _httpx_module()
        async with httpx.AsyncClient(timeout=float(self._settings["request_timeout"])) as client:
            tokens = await self._request_tokens(
                client,
                {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self.redirect_uri,
                    "scope": DEFAULT_SCOPE,
                },
            )
            profile = await self._get_profile(client, str(tokens["access_token"]))
        refresh_token = str(tokens.get("refresh_token") or "")
        if not refresh_token:
            raise E5RenewalError("Microsoft 未返回 refresh_token，请检查应用权限和 offline_access")
        return refresh_token, profile

    async def _request_tokens(
        self, client: Any, form: Mapping[str, str]
    ) -> dict[str, Any]:
        httpx = _httpx_module()
        try:
            response = await client.post(f"{AUTHORITY}/token", data=form)
        except httpx.HTTPError as exc:
            raise E5RenewalError(f"Microsoft OAuth 网络请求失败: {type(exc).__name__}") from exc
        payload = self._json_response(response, "Microsoft OAuth")
        if response.status_code >= 400:
            raise E5RenewalError(self._error_message(payload, response.status_code))
        if str(payload.get("token_type") or "").lower() != "bearer":
            raise E5RenewalError("Microsoft OAuth 返回了无效的 token_type")
        if not payload.get("access_token"):
            raise E5RenewalError("Microsoft OAuth 响应缺少 access_token")
        return payload

    async def _get_profile(
        self, client: Any, access_token: str
    ) -> dict[str, Any]:
        httpx = _httpx_module()
        try:
            response = await client.get(
                f"{GRAPH_API}/me",
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except httpx.HTTPError as exc:
            raise E5RenewalError(f"Microsoft Graph 网络请求失败: {type(exc).__name__}") from exc
        payload = self._json_response(response, "Microsoft Graph")
        if response.status_code >= 400:
            raise E5RenewalError(self._error_message(payload, response.status_code))
        if not payload.get("id"):
            raise E5RenewalError("Microsoft Graph /me 响应缺少用户 ID")
        return payload

    @staticmethod
    def _json_response(response: Any, service: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise E5RenewalError(f"{service} 返回了无效 JSON（HTTP {response.status_code}）") from exc
        if not isinstance(payload, dict):
            raise E5RenewalError(f"{service} 返回了非对象 JSON（HTTP {response.status_code}）")
        return payload

    @staticmethod
    def _error_message(payload: Mapping[str, Any], status_code: int) -> str:
        error = payload.get("error")
        if isinstance(error, dict):
            detail = str(error.get("error_description") or error.get("message") or error.get("code") or "")
        else:
            detail = str(payload.get("error_description") or error or "")
        detail = " ".join(detail.split())[:220]
        return f"Microsoft API 返回 HTTP {status_code}" + (f": {detail}" if detail else "")

    async def _renew_account(
        self, client: Any, account: E5Account
    ) -> RenewalResult:
        httpx = _httpx_module()
        try:
            tokens = await self._request_tokens(
                client,
                {
                    "client_id": account.client_id,
                    "client_secret": account.client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": account.refresh_token,
                    "redirect_uri": self.redirect_uri,
                    "scope": DEFAULT_SCOPE,
                },
            )
            access_token = str(tokens["access_token"])
            try:
                response = await client.get(
                    f"{GRAPH_API}/me/messages",
                    params={"$top": "1", "$select": "id"},
                    headers={"Authorization": f"Bearer {access_token}"},
                )
            except httpx.HTTPError as exc:
                raise E5RenewalError(
                    f"Microsoft Graph 网络请求失败: {type(exc).__name__}"
                ) from exc
            payload = self._json_response(response, "Microsoft Graph")
            if response.status_code >= 400:
                raise E5RenewalError(self._error_message(payload, response.status_code))
            if not isinstance(payload.get("value"), list):
                raise E5RenewalError("Microsoft Graph 邮件列表响应缺少 value 数组")

            refresh_token = str(tokens.get("refresh_token") or account.refresh_token)
            profile = {
                "id": account.ms_user_id,
                "userPrincipalName": account.user_principal_name,
                "displayName": account.display_name,
            }
            await asyncio.to_thread(
                self.store.record_success, account.id, refresh_token, profile
            )
            return RenewalResult(account=account, success=True)
        except E5RenewalError as exc:
            _, disabled = await asyncio.to_thread(
                self.store.record_failure,
                account.id,
                str(exc),
                int(self._settings["max_consecutive_errors"]),
            )
            return RenewalResult(
                account=account, success=False, error=str(exc), disabled=disabled
            )

    async def run_all(self) -> list[RenewalResult]:
        if self._run_lock.locked():
            raise E5RenewalError("已有续订任务正在运行")
        async with self._run_lock:
            accounts = await asyncio.to_thread(self.store.list_enabled)
            if not accounts:
                return []
            concurrency = max(1, int(self._settings["request_concurrency"]))
            semaphore = asyncio.Semaphore(concurrency)
            timeout = float(self._settings["request_timeout"])
            httpx = _httpx_module()
            async with httpx.AsyncClient(timeout=timeout) as client:
                async def run_one(account: E5Account) -> RenewalResult:
                    async with semaphore:
                        return await self._renew_account(client, account)

                return list(await asyncio.gather(*(run_one(account) for account in accounts)))

    async def start(
        self, scheduled_run: Callable[[], Awaitable[None]]
    ) -> None:
        if not bool(self._settings["scheduler_enabled"]):
            logger.info("E5 定时续订已禁用")
            return
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(
            self._scheduler(scheduled_run), name="e5-renewal-scheduler"
        )
        logger.info(
            "E5 定时续订已启动，周期 %.2f 小时",
            float(self._settings["interval_hours"]),
        )

    async def _scheduler(
        self, scheduled_run: Callable[[], Awaitable[None]]
    ) -> None:
        while True:
            await asyncio.sleep(max(1.0, float(self._settings["interval_hours"]) * 3600))
            if not bool(self._settings["scheduler_enabled"]):
                continue
            try:
                await scheduled_run()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("E5 定时续订任务执行失败")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


_service: Optional[E5RenewalService] = None


def configure_e5_renewal(settings: Mapping[str, Any]) -> E5RenewalService:
    """Configure the reload-safe service singleton used by the E5 plugin."""
    global _service
    if _service is None:
        _service = E5RenewalService(settings)
    else:
        _service.configure(settings)
    return _service
