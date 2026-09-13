from core import Context, Keyboard, listener, plugin_config

# 首次运行自动生成 config/wm.json
cfg = plugin_config({
    "prompt": "Please choose:",
    "per_row": 2,             # 每行几个按钮
    "options": [
        {"text": "Option 1", "data": "1"},
        {"text": "Option 2", "data": "2"},
        {"text": "Option 3", "data": "3"},
    ],
})


@listener("wm")
async def wm(ctx: Context, *args, **kwargs):
    options = cfg.get("options") or []
    if not options:
        await ctx.reply_text("没有配置任何选项")
        return

    per_row = max(1, int(cfg.get("per_row") or 1))
    keyboard = Keyboard()
    for index, option in enumerate(options):
        if index and index % per_row == 0:
            keyboard.row()
        keyboard.callback(option.get("text", ""), option.get("data", ""))

    await ctx.reply_text(cfg.get("prompt"), reply_markup=keyboard)
