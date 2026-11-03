"""E5 插件的命令、OAuth 回调与资源生命周期。"""

from __future__ import annotations

import asyncio
import logging
import secrets
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlsplit

from core import (
    WEBHOOK_BOT_KEY,
    Context,
    Keyboard,
    button,
    get_config,
    listener,
    plugin_config,
    shutdown,
    startup,
    webhook_route,
)
from .e5_renewal import (
    E5_CALLBACK_PATH,
    E5RenewalError,
    RenewalResult,
    build_webhook_callback_uri,
    configure_e5_renewal,
    parse_authorization_callback,
    store_call,
)

from utils.plugin_migration import migrate_default_data

logger = logging.getLogger(__name__)

cfg = plugin_config({
    "_comment": "E5 续订插件。OAuth 凭据保存在 database_path 指定的本地 SQLite 文件中。",
    "scheduler_enabled": True,
    "interval_hours": 24,
    "max_accounts_per_user": 5,
    "max_consecutive_errors": 5,
    "request_concurrency": 3,
    "request_timeout": 20.0,
    "database_path": "config/e5/e5_accounts.sqlite3",
    "redirect_uri": "",
})

_SERVICE_SETTINGS = {
    "scheduler_enabled": bool(cfg.get("scheduler_enabled", True)),
    "interval_hours": max(0.01, float(cfg.get("interval_hours", 24))),
    "max_accounts_per_user": max(1, int(cfg.get("max_accounts_per_user", 5))),
    "max_consecutive_errors": int(cfg.get("max_consecutive_errors", 5)),
    "request_concurrency": max(1, int(cfg.get("request_concurrency", 3))),
    "request_timeout": max(1.0, float(cfg.get("request_timeout", 20.0))),
    "database_path": migrate_default_data(cfg, "database_path", "e5_accounts.sqlite3", sqlite=True),
}


def _effective_redirect_uri() -> str:
    configured = str(cfg.get("redirect_uri") or "").strip()
    webhook = get_config().telegram.webhook
    legacy_default = "http://localhost/e5sub"
    if webhook.enable:
        if configured and configured != legacy_default:
            parsed = urlsplit(configured)
            if (
                parsed.scheme.lower() != "https"
                or not parsed.netloc
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or parsed.path != E5_CALLBACK_PATH
            ):
                raise E5RenewalError(
                    "Webhook 自动回调需要 HTTPS 地址，且路径固定为 "
                    f"{E5_CALLBACK_PATH}；请留空 redirect_uri 自动生成"
                )
            return configured
        return build_webhook_callback_uri(webhook.url)
    return configured or legacy_default


def _sync_service_settings() -> None:
    settings = dict(_SERVICE_SETTINGS)
    settings["redirect_uri"] = _effective_redirect_uri()
    service.configure(settings)


service = configure_e5_renewal(
    {**_SERVICE_SETTINGS, "redirect_uri": str(cfg.get("redirect_uri") or "http://localhost/e5sub")}
)


def _user_id(ctx: Context) -> int:
    return int(ctx.user.id) if ctx.user else 0


async def _store_call(
    ctx: Context, operation: Callable[..., Any], *args: Any, **kwargs: Any
) -> tuple[bool, Any]:
    try:
        return True, await store_call(operation, *args, **kwargs)
    except sqlite3.Error:
        logger.exception("E5 数据库操作失败: %s", getattr(operation, "__name__", operation))
        await ctx.reply_text("E5 本地数据库操作失败，请检查 Bot 日志。")
        return False, None


def _is_private(ctx: Context) -> bool:
    return bool(ctx.chat and ctx.chat.type == "private" and ctx.user)


def _parse_callback_url_and_alias(text: str) -> tuple[str, str]:
    try:
        command, remainder = text.split(maxsplit=1)
        callback_url, alias = remainder.split(maxsplit=1)
    except ValueError as exc:
        raise E5RenewalError("格式：/e5finish <授权后完整回调地址> <账户别名>") from exc
    if not command.startswith("/e5finish"):
        raise E5RenewalError("请使用 /e5finish 提交授权回调")
    alias = alias.strip()
    if not alias or len(alias) > 80:
        raise E5RenewalError("账户别名不能为空，且最多 80 个字符")
    return callback_url.strip(), alias


def _parse_client_credentials(text: str) -> tuple[str, str]:
    try:
        _, credentials = text.split(maxsplit=1)
        client_id, client_secret = credentials.split(maxsplit=1)
    except ValueError as exc:
        raise E5RenewalError("格式：/e5bind <client_id> <client_secret>") from exc
    if not client_id or not client_secret:
        raise E5RenewalError("client_id 和 client_secret 均不能为空")
    return client_id, client_secret


def _pending_is_expired(pending: dict[str, str]) -> bool:
    created = datetime.fromisoformat(pending["created_at"])
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created).total_seconds() > 15 * 60


async def _notify_results(results: list[RenewalResult], bot) -> None:
    grouped: dict[int, list[RenewalResult]] = defaultdict(list)
    for result in results:
        grouped[result.account.tg_user_id].append(result)

    for user_id, user_results in grouped.items():
        successes = sum(result.success for result in user_results)
        lines = [
            f"E5 定时续订：{successes}/{len(user_results)} 个账户成功"
        ]
        for result in user_results:
            account = result.account
            if result.success:
                lines.append(f"✅ {account.alias}")
            else:
                state = "（已暂停，连续错误达到上限）" if result.disabled else ""
                lines.append(f"❌ {account.alias}{state}：{result.error[:180]}")
        try:
            await bot.send_message(chat_id=user_id, text="\n".join(lines))
        except Exception:
            logger.exception("发送 E5 续订结果失败，Telegram user id=%s", user_id)


async def _scheduled_run(bot) -> None:
    results = await service.run_all()
    if results:
        await _notify_results(results, bot)


@startup
async def _start_e5_renewal(application=None) -> None:
    if application is None:
        raise RuntimeError("E5 定时续订启动钩子缺少 Application")
    await service.start(lambda: _scheduled_run(application.bot))


@shutdown
async def _stop_e5_renewal() -> None:
    await service.stop()


async def _notify_binding(bot: Any, user_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id=user_id, text=text)
    except Exception:
        logger.exception("发送 E5 OAuth 绑定通知失败，Telegram user id=%s", user_id)


@webhook_route(E5_CALLBACK_PATH)
async def e5_oauth_callback(request: Any):
    from aiohttp import web

    def response(text: str, status: int = 200) -> web.Response:
        return web.Response(
            text=text,
            status=status,
            content_type="text/plain",
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

    try:
        _sync_service_settings()
    except E5RenewalError as exc:
        logger.error("E5 OAuth 回调配置错误: %s", exc)
        return response("E5 OAuth callback is not configured.", 503)

    query = request.rel_url.query
    states = query.getall("state", [])
    if len(states) != 1 or not states[0]:
        return response("Missing or invalid OAuth state.", 400)
    state = states[0]
    try:
        pending = await store_call(service.store.get_pending_by_state, state)
        if pending is None or not secrets.compare_digest(state, pending["state"]):
            return response("This authorization request is no longer valid.", 400)
        if _pending_is_expired(pending):
            await store_call(service.store.delete_pending_by_state, state)
            await _notify_binding(
                request.app[WEBHOOK_BOT_KEY],
                int(pending["tg_user_id"]),
                "E5 授权已超时，请重新执行 /e5bind。",
            )
            return response("This authorization request has expired.", 400)

        errors = query.getall("error_description", []) or query.getall("error", [])
        if errors:
            await store_call(service.store.delete_pending_by_state, state)
            message = f"Microsoft 授权失败：{errors[0][:220]}"
            await _notify_binding(
                request.app[WEBHOOK_BOT_KEY], int(pending["tg_user_id"]), message
            )
            return response(message, 400)

        codes = query.getall("code", [])
        if len(codes) != 1 or not codes[0]:
            return response("Missing authorization code.", 400)
        refresh_token, profile = await service.exchange_code(
            pending["client_id"], pending["client_secret"], codes[0]
        )
        display_name = str(profile.get("displayName") or "").strip()
        principal = str(profile.get("userPrincipalName") or "").strip()
        alias = (display_name or principal or f"E5 {pending['client_id'][:8]}")[:80]
        account = await store_call(
            service.store.add_account,
            tg_user_id=int(pending["tg_user_id"]),
            alias=alias,
            client_id=pending["client_id"],
            client_secret=pending["client_secret"],
            refresh_token=refresh_token,
            profile=profile,
        )
        await store_call(service.store.delete_pending_by_state, state)
    except E5RenewalError as exc:
        logger.info("E5 OAuth 自动绑定失败: %s", exc)
        await _notify_binding(
            request.app[WEBHOOK_BOT_KEY],
            int(pending["tg_user_id"]),
            f"Microsoft 授权绑定失败：{exc}",
        )
        return response(f"Microsoft authorization failed: {exc}", 400)
    except (sqlite3.Error, OSError, ValueError):
        logger.exception("处理 E5 OAuth 回调失败")
        return response("E5 authorization could not be completed. Check the bot logs.", 500)
    except Exception:
        logger.exception("处理 E5 OAuth 回调时发生未处理错误")
        return response("E5 authorization could not be completed. Check the bot logs.", 500)

    await _notify_binding(
        request.app[WEBHOOK_BOT_KEY],
        account.tg_user_id,
        f"Microsoft 账户绑定成功：{account.alias}\n"
        f"Microsoft 账户：{account.display_name or account.user_principal_name or account.ms_user_id}\n"
        "凭据已保存在 Bot 本机 SQLite 数据库中。",
    )
    return response("Microsoft account connected. You can return to Telegram.")


@listener("e5help")
async def e5_help(ctx: Context, *args, **kwargs):
    try:
        _sync_service_settings()
    except E5RenewalError as exc:
        await ctx.reply_text(f"E5 回调配置错误：{exc}")
        return
    await ctx.reply_text(
        "E5 续订指令（请在私聊中管理账户）：\n"
        "/e5bind <client_id> <client_secret> - 绑定并授权 Microsoft 账户\n"
        "/e5finish <回调地址> <别名> - 手动完成授权（自动回调不可达时）\n"
        "/e5accounts - 查看自己绑定的账户\n"
        "/e5unbind - 选择并解绑自己的账户\n"
        "/e5cancel - 取消当前绑定流程\n"
        "/e5run - 管理员手动执行全部账户续订\n\n"
        f"当前 OAuth redirect_uri：{service.redirect_uri}\n"
        "应用需配置 delegated Mail.Read、User.Read 权限；授权请求会额外请求 "
        "offline_access scope 以获取 refresh token。"
    )


@listener("e5bind")
async def e5_bind(ctx: Context, *args, **kwargs):
    if not _is_private(ctx):
        await ctx.reply_text("账户绑定仅支持与 Bot 私聊。")
        return
    message = ctx.message
    if message is None or not message.text:
        await ctx.reply_text("格式：/e5bind <client_id> <client_secret>")
        return
    try:
        client_id, client_secret = _parse_client_credentials(message.text)
        _sync_service_settings()
    except E5RenewalError as exc:
        await ctx.reply_text(str(exc))
        return

    owner_id = _user_id(ctx)
    ok, count = await _store_call(ctx, service.store.count_for_user, owner_id)
    if not ok:
        return
    if count >= int(cfg.get("max_accounts_per_user", 5)):
        await ctx.reply_text(f"账户数量已达上限（{cfg.get('max_accounts_per_user', 5)}）。")
        return

    state = secrets.token_urlsafe(32)
    ok, _ = await _store_call(
        ctx, service.store.save_pending, owner_id, client_id, client_secret, state
    )
    if not ok:
        return
    authorize_url = service.authorize_url(client_id, state)
    callback = urlsplit(service.redirect_uri)
    if get_config().telegram.webhook.enable:
        instructions = (
            "点击授权并使用对应的 E5 主账号或同租户账号登录。授权完成后，"
            "浏览器会自动完成绑定；账户别名使用 Microsoft 显示名称。"
        )
    else:
        instructions = (
            "点击授权并使用对应的 E5 主账号或同租户账号登录。授权完成后，复制浏览器地址栏"
            "中的完整回调地址，执行 /e5finish <回调地址> <账户别名>。"
        )
    await ctx.reply_text(
        "请在 Microsoft Entra 应用注册中配置与下方完全一致的 redirect URI：\n"
        f"{service.redirect_uri}\n\n{instructions}\n"
        f"回调主机：{callback.netloc}",
        reply_markup=Keyboard().url("Microsoft 登录授权", authorize_url),
    )


@listener("e5finish")
async def e5_finish(ctx: Context, *args, **kwargs):
    if not _is_private(ctx):
        await ctx.reply_text("账户授权仅支持与 Bot 私聊。")
        return
    message = ctx.message
    if message is None or not message.text:
        await ctx.reply_text("请使用 /e5finish <回调地址> <账户别名>。")
        return
    try:
        _sync_service_settings()
        callback_url, alias = _parse_callback_url_and_alias(message.text)
        callback = parse_authorization_callback(callback_url, service.redirect_uri)
        pending = await store_call(service.store.get_pending, _user_id(ctx))
        if pending is None:
            raise E5RenewalError("找不到待完成的绑定，请重新执行 /e5bind")
        if _pending_is_expired(pending):
            await store_call(
                service.store.delete_pending_by_state, pending["state"]
            )
            raise E5RenewalError("授权已超时，请重新执行 /e5bind")
        if not secrets.compare_digest(callback.state, pending["state"]):
            raise E5RenewalError("OAuth state 不匹配，请重新执行 /e5bind")
        if callback.error:
            raise E5RenewalError(f"Microsoft 授权失败：{callback.error[:220]}")

        refresh_token, profile = await service.exchange_code(
            pending["client_id"], pending["client_secret"], callback.code
        )
        account = await store_call(
            service.store.add_account,
            tg_user_id=_user_id(ctx),
            alias=alias,
            client_id=pending["client_id"],
            client_secret=pending["client_secret"],
            refresh_token=refresh_token,
            profile=profile,
        )
        await store_call(
            service.store.delete_pending_by_state, callback.state
        )
    except (E5RenewalError, ValueError, sqlite3.Error, OSError) as exc:
        await ctx.reply_text(f"绑定失败：{exc}")
        return

    display_name = str(profile.get("displayName") or "")
    principal = str(profile.get("userPrincipalName") or "")
    await ctx.reply_text(
        f"绑定成功：{account.alias}\n"
        f"Microsoft 账户：{display_name or principal or account.ms_user_id}\n"
        "凭据已保存在本机 SQLite 数据库中。"
    )


@listener("e5accounts")
async def e5_accounts(ctx: Context, *args, **kwargs):
    if not _is_private(ctx):
        await ctx.reply_text("账户信息仅支持与 Bot 私聊查看。")
        return
    ok, accounts = await _store_call(
        ctx, service.store.list_for_user, _user_id(ctx)
    )
    if not ok:
        return
    if not accounts:
        await ctx.reply_text("还没有绑定账户。使用 /e5bind 开始绑定。")
        return

    lines = [f"已绑定账户：{len(accounts)}/{cfg.get('max_accounts_per_user', 5)}"]
    for account in accounts:
        state = "运行中" if account.enabled else "已暂停"
        identity = account.user_principal_name or account.display_name or account.ms_user_id
        last_success = account.last_success_at or "尚未执行"
        lines.append(
            f"\n#{account.id} {account.alias}（{state}）\n"
            f"Microsoft：{identity or '未返回账户信息'}\n"
            f"最近成功：{last_success}；连续失败：{account.error_count}"
        )
    await ctx.reply_text("\n".join(lines))


@listener("e5unbind")
async def e5_unbind(ctx: Context, *args, **kwargs):
    if not _is_private(ctx):
        await ctx.reply_text("账户解绑仅支持与 Bot 私聊。")
        return
    ok, accounts = await _store_call(
        ctx, service.store.list_for_user, _user_id(ctx)
    )
    if not ok:
        return
    if not accounts:
        await ctx.reply_text("没有可解绑的账户。")
        return

    keyboard = Keyboard()
    for index, account in enumerate(accounts):
        if index:
            keyboard.row()
        keyboard.callback(f"解绑 {account.alias}", f"e5:unbind:{account.id}")
    await ctx.reply_text("选择要解绑的账户：", reply_markup=keyboard)


@button("e5:unbind:")
async def e5_unbind_account(ctx: Context, payload: str):
    query = ctx.query
    if query is None or not _is_private(ctx):
        await ctx.answer_query("请在 Bot 私聊中操作。", show_alert=True)
        return
    try:
        account_id = int(payload)
    except ValueError:
        await ctx.answer_query("账户编号无效。", show_alert=True)
        return

    try:
        removed = await store_call(
            service.store.delete_owned_account, account_id, _user_id(ctx)
        )
    except sqlite3.Error:
        logger.exception("解绑 E5 账户失败")
        await ctx.answer_query("数据库操作失败，请稍后重试。", show_alert=True)
        return
    if not removed:
        await ctx.answer_query("找不到该账户，或它不属于你。", show_alert=True)
        return
    await ctx.answer_query("账户已解绑。")
    await ctx.reply_text(f"已解绑账户 #{account_id}。")


@listener("e5cancel")
async def e5_cancel(ctx: Context, *args, **kwargs):
    if not _is_private(ctx):
        await ctx.reply_text("绑定流程仅支持与 Bot 私聊。")
        return
    ok, _ = await _store_call(ctx, service.store.delete_pending, _user_id(ctx))
    if not ok:
        return
    await ctx.reply_text("已取消待完成的绑定流程。")


@listener("e5run")
async def e5_run(ctx: Context, *args, **kwargs):
    if not _is_private(ctx):
        await ctx.reply_text("手动续订仅支持与 Bot 私聊。")
        return
    user = ctx.user
    if user is None or not user.is_owner:
        await ctx.reply_text("只有 Bot 主人可以手动执行全局续订任务。")
        return
    await ctx.reply_text("续订任务已开始，完成后会向各账户所有者发送结果。")
    try:
        results = await service.run_all()
    except (E5RenewalError, sqlite3.Error, OSError) as exc:
        await ctx.reply_text(f"续订任务失败：{exc}")
        return
    if not results:
        await ctx.reply_text("当前没有启用的 E5 账户。")
        return
    await _notify_results(results, ctx.bot)
    successes = sum(result.success for result in results)
    await ctx.reply_text(f"全局任务完成：{successes}/{len(results)} 个账户成功。")
