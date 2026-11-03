from core import Context, listener


@listener("check")
async def check(ctx: Context, *args, **kwargs):
    arguments = "\n  ".join(args)
    keyword_arguments = "\n  ".join(f"{key}={value}" for key, value in kwargs.items())
    await ctx.reply_text(
        f"You have input {len(args)} arguments and {len(kwargs)} keyword arguments.\n\n"
        f"Arguments:\n  {arguments}\nKeyword Arguments:\n  {keyword_arguments}"
    )
