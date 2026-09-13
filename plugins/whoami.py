from core import Context, listener, plugin_config

# 首次运行自动生成 config/whoami.json
cfg = plugin_config({
    # 支持 {id} / {username} / {full_name} 三个占位符
    "template": "your user id is: {id}\nyour username is: {username}",
    "show_full_name": False,
})


@listener("whoami")
async def whoami(ctx: Context, *args, **kwargs):
    user = ctx.user
    if user is None:
        return

    text = cfg.get("template").format(
        id=user.id,
        username=user.username,
        full_name=user.full_name,
    )
    if cfg.get("show_full_name") and user.full_name:
        text = f"{text}\nyour name is: {user.full_name}"

    await ctx.send_text(user.id, text)
