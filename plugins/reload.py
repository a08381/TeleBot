from core import Context, listener, reload_all_plugins


def _is_allowed(ctx: Context) -> bool:
    user = ctx.user
    if user is None:
        return False
    return user.is_owner


@listener("reload")
async def reload(ctx: Context, *args, **kwargs):
    if not _is_allowed(ctx):
        return

    loaded = reload_all_plugins()
    await ctx.reply_text(f"Reload Complete. ({len(loaded)} plugins)")