from core import Context, listener, reload_all_plugins
from core.lifecycle import PluginShutdownError
from core.runtime import PluginsReloading


def _is_allowed(ctx: Context) -> bool:
    user = ctx.user
    if user is None:
        return False
    return user.is_owner


@listener("reload")
async def reload(ctx: Context, *args, **kwargs):
    if not _is_allowed(ctx):
        return

    try:
        loaded = await reload_all_plugins()
    except (PluginShutdownError, PluginsReloading) as exc:
        await ctx.reply_text(str(exc))
        return
    await ctx.reply_text(f"重载完成，已加载 {len(loaded)} 个插件。")