from core import Context, listener


@listener("whoami")
async def whoami(ctx: Context, *args, **kwargs):
    user = ctx.user
    if user is None:
        return

    text = "your user id is: {id}\nyour username is: {username}".format(
        id=user.id,
        username=user.username,
        full_name=user.full_name,
    )
    if user.full_name:
        text = f"{text}\nyour name is: {user.full_name}"

    await ctx.send_text(user.id, text)
