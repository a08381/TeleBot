from core import Context, listener, plugin_config, reload_all_plugins

# 首次运行自动生成 config/reload.json
cfg = plugin_config({
    # 除了主配置 config.json 的 owner_ids，这里还能额外放行几个管理员
    "extra_owner_ids": [],
    "message": "Reload Complete. ({count} plugins)",
})


def _is_allowed(ctx: Context) -> bool:
    user = ctx.user
    if user is None:
        return False
    return user.is_owner or user.id in set(cfg.get("extra_owner_ids") or [])


@listener("reload")
async def reload(ctx: Context, *args, **kwargs):
    if not _is_allowed(ctx):
        return

    loaded = reload_all_plugins()
    await ctx.reply_text(cfg.get("message").format(count=len(loaded)))
