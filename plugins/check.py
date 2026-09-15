from core import Context, listener


@listener("check")
async def check(ctx: Context, *args, **kwargs):
    await ctx.reply_text(f"You have input {len(args)} arguments and {len(kwargs)} keyword arguments.\n\nArguments:\n  {"\n  ".join(args)}\nKeyword Arguments:\n  {"\n  ".join(f'{k}={v}' for k, v in kwargs.items())}")
