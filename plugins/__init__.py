"""业务插件目录。

约定：
- 每个 .py 文件是一个插件，启动时自动被 core.loader 扫描加载
- 只用 `from core import ...`（以及需要时的 `from utils import ...`），
  不要 import telegram —— 所有 Telegram 交互都走 core.Context
- 指令处理函数签名：async def f(ctx: core.Context, *args, **kwargs)
"""
