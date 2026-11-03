# TeleBot

基于 `python-telegram-bot` 的异步 Telegram Bot。插件通过装饰器注册命令、按钮、生命周期钩子与 HTTP 回调；框架负责 Telegram 交互、分发和启动。内置 Warframe Market 账号/订单管理、Microsoft 365 E5 账户与定时 Graph 活动，以及图站媒体查询。

支持长轮询与 webhook。插件通常只需导入 `core` 和 `utils`，不直接依赖 Telegram SDK。

## 快速开始

需要 **Python 3.10 或更高版本**，以及 Telegram Bot Token。依赖版本范围见 [requirements.txt](requirements.txt)。请从项目根目录执行下面的命令。

### 安装

Linux / macOS：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp config.example.json config.json
```

Windows PowerShell：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config.example.json config.json
```

编辑 [config.json](config.json)，填写 `telegram.token`。`owner_ids` 填你自己的 Telegram 数字用户 ID；默认空列表表示没有管理员。可以先在私聊中发送 `/whoami` 获取 ID，随后修改配置并重启。

### 初始化与启动

```bash
# 可在填写 Bot Token 前执行，不连接 Telegram，也不启动后台任务
python main.py --init-config

# 默认使用长轮询
python main.py

# 查看命令行参数
python main.py --help
```

PowerShell 中未激活虚拟环境时，将 `python` 换成 `.\.venv\Scripts\python.exe`。

初始化会导入全部插件，生成 [config/yiff/config.json](config/yiff/config.json)、[config/wm/config.json](config/wm/config.json)、[config/e5/config.json](config/e5/config.json)，并创建 E5 的 SQLite 数据库。基础工具插件没有自己的配置文件。

正常启动会执行插件启动钩子：图站帖子池首次补充、WM 客户端与可选物品清单预热、E5 定时任务初始化。因此，启动阶段也可能访问配置的外部服务；`--init-config` 则不执行这些钩子。

## 主配置与部署

主配置仅包含 Telegram、管理员与日志设置。完整模板见 [config.example.json](config.example.json)。

```json
{
  "telegram": {
    "token": "填写 Bot Token",
    "host": "https://api.telegram.org",
    "webhook": {
      "enable": false,
      "url": "",
      "port": 7755,
      "path": "/telegram",
      "secret_token": ""
    }
  },
  "owner_ids": [],
  "log_level": "INFO",
  "log_dir": "logs",
  "log_split_streams": true,
  "log_format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
}
```

| 配置项 | 行为 |
| --- | --- |
| `telegram.token` | 必填，由 BotFather 创建 Bot 后取得 |
| `telegram.host` | Telegram Bot API 根地址；自建 API 服务或反向代理时修改 |
| `telegram.webhook.enable` | `false` 使用长轮询，`true` 使用项目内置 aiohttp 监听器 |
| `telegram.webhook.url` | 外部 HTTPS 地址；与内置 E5 回调配合时使用主机根地址，例如 `https://bot.example.com` |
| `telegram.webhook.port` | 内部 HTTP 监听端口，默认 `7755`；监听 `0.0.0.0` |
| `telegram.webhook.path` | Telegram 更新路径，默认 `/`；示例使用 `/telegram`，不得与插件路由冲突 |
| `telegram.webhook.secret_token` | 可选，校验 Telegram 的 `X-Telegram-Bot-Api-Secret-Token` 请求头 |
| `owner_ids` | 管理员 ID 数组；未配置或显式设为 `[]` 时不授予管理员权限 |
| `log_level` / `log_dir` | 日志级别和目录；相对日志路径基于项目根目录 |
| `log_split_streams` | 默认将低于 WARNING 的日志写入 stdout，WARNING 及以上写入 stderr |

日志写入 [logs/telebot.log](logs/telebot.log)，按天轮转，最多保留 10 份备份。文件和控制台都受 `log_level` 过滤。根配置启动后会缓存，修改后需要重启。

兼容旧配置中的顶层 `t_token`、`t_host`、`webhook` 和 `owner_id`；优先使用新 `telegram` 段中填写的值。旧配置中显式填写的管理员 ID 会继续生效。

### webhook

将 `enable` 设为 `true`，填写外部 HTTPS 地址与内部端口。内置服务不处理 TLS，需由反向代理完成 HTTPS 终止，并把请求转发到 Bot 的 HTTP 端口。

使用上述 `/telegram` 配置时，应同时转发：

| 外部请求 | 用途 |
| --- | --- |
| `POST https://bot.example.com/telegram` | Telegram 更新 |
| `GET https://bot.example.com/e5/callback` | E5 OAuth 自动回调 |

Telegram 更新会先校验 secret、Content-Type、JSON 和 `update_id`，再进入 Application 更新队列。E5 回调独立校验 OAuth state，不使用 Telegram webhook secret。内置 HTTP 服务关闭访问日志；反向代理也应避免记录带授权码的完整回调 URL。

长轮询模式不启动 HTTP 监听器，E5 使用手动完成授权的流程。

## 内置命令

### 基础工具

| 命令 | 功能与权限 |
| --- | --- |
| `/ping` | 输出本机当前时间与 Telegram 消息时间戳的差值，单位秒；不是网络 RTT 测试 |
| `/whoami` | 将用户 ID、username、姓名发送到发起者私聊；先与 Bot 建立私聊 |
| `/check [参数 ...] [k=v ...]` | 回显分发器解析的位置参数和键值参数 |
| `/reload` | 仅管理员，重新加载所有插件并返回成功加载数量 |

### Warframe Market

公开查价无需登录。绑定会话按 Telegram 用户 ID 保存，操作自己的账号；若多个 Telegram 用户绑定同一个 WM 账号，远端操作仍会相互影响。

| 命令 | 功能与权限 |
| --- | --- |
| `/wm` | 打开账号与订单管理面板 |
| `/wm online\|ingame\|invisible\|offline` | 设置自己的状态 |
| `/wmstatus [状态]` | 查询或设置自己的状态，需要已绑定账号 |
| `/wmlogin <邮箱> <密码>` | 绑定自己的 WM 账号，默认允许普通用户使用；请在私聊中操作 |
| `/wmlogin` | 仅管理员，使用配置中的预设账号绑定 |
| `/wmlogout [TG用户ID]` | 默认解绑自己；指定用户需要管理员权限且配置允许 |
| `/wmwho` | 普通用户查看自己，管理员查看全部绑定 |
| `/wmitems [页码]` | 分页查看自己的卖单，包括隐藏卖单；命令参数从 `0` 开始，界面页码从 `1` 开始；也支持 `page=N` |
| `/wmvisible` | 立即把自己的全部隐藏订单设为可见，包含买单和卖单，没有二次确认 |
| `/wmreprice` | 预览可见卖单改价计划，点击确认后执行 |
| `/price <物品> [platform=pc] [crossplay=false] [rank=5]` | 公开查价；别名 `/wmprice`，也接受 `plat`、`r` 参数 |
| `/wmrefresh` | 仅管理员，强制刷新物品清单与名称索引 |
| `/wmdiag` | 仅管理员，检查接口；已绑定时还会将账号设为 online 并保持连接，不是纯只读检查 |

查价支持名称、slug、昵称、Prime/部位后缀和等级。解析先尝试 `aliases`，再尝试官方名称、组合名称与模糊候选；中文匹配取决于上游物品清单包含的名称。多个候选时通过按钮选择。

```text
/price nikana prime set
/price 充沛 rank=5
/price 满级优雅
/price volt prime set platform=pc crossplay=true
/wmitems 0
```

改价目标是排除自己的订单后、符合等级与在线筛选条件的最低竞争卖价，**既可能降价，也可能涨价**，不是自动减 1。计划有效期 10 分钟，一次性使用；确认时重新检查自己的订单与市场价，预览后发生变化的项目会跳过。仅处理可见卖单，不包含持续自动改价任务，也不创建或删除订单。统计展示的是上游最新时间桶的聚合数据，不是单笔成交。

主要配置见自动生成的 [config/wm/config.json](config/wm/config.json)：

| 配置项 | 默认值 / 说明 |
| --- | --- |
| `account.email` / `account.password` | 空；可预设管理员账号 |
| `account.user_id` | `0`，使用 `owner_ids` 中第一个用户 |
| `autologin` | `true`，启动时仅在目标用户尚未绑定时自动登录 |
| `bind.allow_all` | `true`，设为 `false` 后仅管理员可绑定 |
| `bind.owner_can_unbind_all` | `true`，允许管理员指定用户解绑 |
| `platform` / `crossplay` / `language` | `pc` / `false` / `en` |
| `rate_limit.rps` / `rate_limit.burst` | `3` / `3`，多个账号共享限流器 |
| `cache.items_ttl` / `cache.orders_ttl` | `86400` / `60` 秒；名称索引不是定时自动刷新，使用 `/wmrefresh` 更新 |
| `api.timeout` | `20` 秒，HTTP 和状态 WebSocket 使用此值 |
| `price.only_online` / `price.top_n` | `true` / `5`，默认只统计 online、ingame 的可见订单 |
| `my_listings.page_size` | `8`，有效范围 1–20 |
| `session_file` | `config/wm/wm_sessions.json`；默认位置按项目根目录定位，自定义相对路径基于进程当前工作目录 |
| `aliases` | 内置昵称映射，可自行扩展 |
| `browser` | HTTP 请求的浏览器指纹、Cookie、代理等选项，不等于状态 WebSocket 配置 |

登录与部分读取包含 v1/v2 回退；自己的订单使用 v2 `/orders/my`，修改使用 v2 `/order/{id}`。外部接口是否可用取决于 WM 服务端，代码中的回退不保证所有接口始终可用。

online、ingame、invisible 通过 `wss://warframe.market/socket-v2` 为各用户保持独立连接。offline 关闭本 Bot 的对应连接，不保证其他客户端会话也离线。重启、断网或客户端配置重建后没有自动恢复在线连接的后台机制，需要重新设置状态。旧 `status_write.mode=off` 可关闭常规状态切换，但 `/wmdiag` 仍会直接检查在线连接。

### Microsoft 365 E5

通过 Microsoft OAuth 绑定账户，定时刷新 access token 并调用 Graph `/me/messages?$top=1&$select=id`。只请求最多一个邮件 ID，不读取或保存邮件正文，也不发送邮件。**Graph 活动成功不代表开发者订阅一定续订。**

1. 在 Microsoft Entra 注册应用，创建 client secret，配置 Web 回调 URI，以及 delegated `User.Read`、`Mail.Read` 权限。插件授权时还请求 `openid`、`offline_access`。
2. webhook 模式下，`redirect_uri` 留空时使用 `https://你的域名/e5/callback`；确保 Microsoft 应用中登记的 URI 完全一致，且反向代理已转发该路径。
3. 在 Bot 私聊中执行 `/e5bind`，打开授权链接。webhook 模式自动完成绑定；轮询模式使用完整回调 URL 手动执行 `/e5finish`。

```text
/e5bind <client_id> <client_secret>
/e5finish <完整回调URL> <账户别名>
/e5accounts
/e5unbind
```

轮询模式下，空 `redirect_uri` 使用 `http://localhost/e5sub`。项目不会启动该 localhost 服务；授权后即使浏览器显示连接失败，也可以从地址栏复制带 `code`、`state` 的完整地址交给 `/e5finish`。自定义 URI 同样必须与应用注册配置一致。

| 命令 | 功能与权限 |
| --- | --- |
| `/e5help` | 查看帮助与当前有效回调地址 |
| `/e5bind <client_id> <client_secret>` | 私聊，开始绑定；每用户只有一个待完成绑定，新请求替换旧请求，有效期 15 分钟 |
| `/e5finish <完整回调URL> <账户别名>` | 私聊，手动完成绑定；别名可含空格，长度 1–80 |
| `/e5accounts` | 私聊，查看自己的账户、最近成功时间与失败次数 |
| `/e5unbind` | 私聊，通过按钮选择并删除自己的账户 |
| `/e5cancel` | 私聊，取消自己尚未完成的绑定 |
| `/e5run` | 管理员且私聊，立即执行全部已启用账户，并通知各账户所有者 |

主要配置见 [config/e5/config.json](config/e5/config.json)：

| 配置项 | 默认值 / 说明 |
| --- | --- |
| `scheduler_enabled` | `true` |
| `interval_hours` | `24`，启动后先等待一个完整周期，再首次执行；不是固定钟点任务 |
| `max_accounts_per_user` | `5` |
| `max_consecutive_errors` | `5`，达到后暂停账户；设为 `0` 或负数不自动暂停 |
| `request_concurrency` / `request_timeout` | `3` / `20` 秒 |
| `database_path` | `config/e5/e5_accounts.sqlite3`，相对路径基于项目根目录 |
| `redirect_uri` | 空，按当前运行模式推导 |

同一 Telegram 用户与同一 `client_id` 只允许一条账户记录。连续失败后账户暂停但不删除；当前没有单独恢复命令，处理授权问题后可解绑重绑。`/e5run` 不执行已暂停账户。

应用密钥、refresh token、账户和待绑定状态保存在本地明文 SQLite 中；含密钥的 `/e5bind` 消息也不会自动删除。请保护配置、数据库及备份；Windows 上需单独设置文件 ACL。

### 图站媒体查询

| 命令 | 功能与权限 |
| --- | --- |
| `/yiff [标签 ...]` | 按标签查询媒体，不带位置参数时使用 `default_tags` |
| `/fp`、`/yiff_fp` | 仅管理员，显示站点、实际 UA、指纹状态和 Cookie 名称，不显示 Cookie 值 |

配置见 [config/yiff/config.json](config/yiff/config.json)。默认站点为 `https://e621.net`，可按需要改为 `https://e926.net`。请自行检查生成的 `default_tags` 与目标站点内容设置。

| 配置项 | 默认值 / 说明 |
| --- | --- |
| `site` / `default_tags` | 站点地址与无参数查询时的标签 |
| `user_agent` | 标识应用的 UA，可填写自己的应用与联系方式 |
| `pool.size` / `pool.low_water` | `40` / `10`，池内保存帖子元数据，不预下载媒体文件 |
| `pool.refill_interval` / `pool.min_interval` | `30` / `1.1` 秒 |
| `pool.cache_ttl` | `300` 秒 |
| `upload_limit_mb` | `20`，仅检查本机下载后的上传回退，不是下载流量或内存占用的硬上限 |
| `enable_next_button` | `true`，启用换图按钮 |
| `caption` | `帖子 {id}` |
| `browser` | 浏览器指纹、Cookie、UA、代理、超时等 HTTP 选项 |

首次发送依次尝试 photo URL、document URL、下载后上传；photo 尝试受 `photo_exts` 限制。换图编辑没有同样的完整回退流程，按钮也不限制为原请求者点击。换图记录保存在插件内存中，重载或重启后旧按钮可能失效。

当前非空标签查询走搜索缓存/请求路径，只有空标签才消费预热队列，因此默认调用不保证直接从预热池取出结果。

`browser.impersonate` 非空时尝试使用 `curl_cffi`；为空或该依赖未安装时使用 httpx。依赖清单已包含 `curl_cffi`。这只是 HTTP 客户端能力，不保证通过站点挑战或取得登录权限。`browser.cookie` 字符串与 `browser.cookies` 字典会合并，同名时字符串覆盖；Cookie 按站点域和显式配置的额外域注入。

## 插件开发与项目结构

```text
TeleBot/
├── main.py                  命令行入口
├── config.example.json      主配置模板
├── requirements.txt         运行依赖
├── core/
│   ├── bot.py               配置、插件、分发器的装配与启动
│   ├── server.py            webhook HTTP 服务与 Application 启停
│   ├── dispatcher.py        命令/按钮注册、解析与分发
│   ├── loader.py            顶层入口扫描、整包导入与卸载
│   ├── runtime.py           调用登记与重载互斥
│   ├── lifecycle.py         插件启动/停止钩子
│   ├── webhook.py           插件 GET 路由与共享 Bot 键
│   ├── context.py           Telegram 上下文门面
│   ├── keyboard.py          内联键盘构造器
│   ├── errors.py            BotError
│   └── logging_setup.py     日志配置
├── plugins/
│   ├── check.py / ping.py / whoami.py / reload.py
│   ├── e5/
│   │   ├── __init__.py      包入口，setup() 导入同名处理器
│   │   ├── e5.py            指令、OAuth 回调与生命周期
│   │   └── e5_renewal.py    SQLite、OAuth 与定时任务
│   ├── wm/
│   │   ├── __init__.py
│   │   ├── wm.py            指令、按钮与生命周期
│   │   ├── wm_api.py        HTTP、WebSocket、会话与缓存
│   │   └── wm_items.py      名称索引与物品查询
│   └── yiff/
│       ├── __init__.py
│       ├── yiff.py          指令、按钮与生命周期
│       ├── http_pool.py     图站私有 HTTP 客户端
│       └── posts_pool.py    帖子池与搜索缓存
├── utils/                   共享配置与 browser_profile HTTP 客户端工厂
├── config/
│   ├── e5/
│   │   ├── config.json
│   │   └── e5_accounts.sqlite3
│   ├── wm/
│   │   ├── config.json
│   │   └── wm_sessions.json
│   └── yiff/
│       └── config.json
└── tests/                   单元测试与隔离插件重载测试
```

### 单文件与插件包

加载器只扫描 [plugins/](plugins/) 顶层入口，同时支持单文件插件和带有包入口的插件目录；以下划线开头的顶层模块会跳过。包内工具、测试和子包不会自动递归导入，由入口或处理器按需引用。同一插件名应只选择一种布局，避免单文件与同名包并存。

简单插件可以直接保存为 [plugins/hello.py](plugins/hello.py)：

```python
from core import Context, Keyboard, button, listener, plugin_config

cfg = plugin_config({"greeting": "你好"})


@listener("hello")
async def hello(ctx: Context, *args, **kwargs):
    name = ctx.user.full_name if ctx.user else "访客"
    keyboard = Keyboard().callback("打招呼", "hello:again")
    await ctx.reply_text(f"{cfg.greeting}，{name}", reply_markup=keyboard)


@button("hello:")
async def hello_button(ctx: Context, payload: str):
    await ctx.answer_query(f"收到：{payload}")
```

需要私有工具时，将上面的处理器放到 [plugins/hello/hello.py](plugins/hello/hello.py)，并增加 [plugins/hello/__init__.py](plugins/hello/__init__.py)：

```python
def setup() -> None:
    from . import hello  # 导入时注册命令、按钮及生命周期钩子
```

`setup()` 是可选的同步入口，每轮加载只执行一次；异步初始化放在 `@startup` 中。也可以直接在包入口注册处理器，但内置三个插件使用延迟导入，单独导入私有工具时不会顺带注册指令或生成配置、数据库。

私有工具与服务放在同一个插件目录中，通过 `from . import client` 等相对导入引用；更深的工具子包同样支持。不要在 `core`、共享 `utils` 或其他长生命周期对象中保留插件工具的引用，否则删除模块缓存后这些外部引用仍可能指向旧对象。真正共享的配置工具、客户端工厂继续放在 `utils`。

### 框架接口

命令按空白拆分参数，`key=value` 作为关键字参数，其余作为位置参数；没有 shell 风格引号解析。`/cmd@BotName` 只响应本 Bot，命令和 Bot 名匹配不区分大小写。`ctx.args`、`ctx.kwargs` 提供参数副本。按钮先匹配最长字面前缀，否则按注册顺序尝试正则并传入捕获组。

`Context` 提供用户/会话/消息门面，以及 `reply_text`、`send_text`、`reply_photo`、`send_photo`、`reply_document`、`send_document`、`edit_photo`、`edit_document`、`answer_query`。底层 Telegram 调用异常统一包装为 `BotError`。需高级 SDK 功能时可访问 `ctx.bot`、`ctx.update`；`user`、`chat`、`message`、`query` 可能为 `None`。

### 配置目录与旧数据迁移

`plugin_config()` 根据顶层插件名生成配置：`plugins.hello`、`plugins.hello.hello` 和 `plugins.hello.tools.client` 都默认使用 [config/hello/config.json](config/hello/config.json)。配置递归补齐默认值、保留额外字段，并按默认类型转换。返回的字典、列表都是独立副本：修改配置应使用 `set()` / `update()`，而不是直接修改 `cfg.get()` 的返回值。写入使用临时文件替换；`reload()` 重新读盘并补写缺失字段，`reset()` 恢复默认值。

插件内可获取专属配置目录，用于保存缓存、数据库等文件：

```python
from core import get_plugin_config_dir, plugin_config

cfg = plugin_config({"timeout": 20})
directory = get_plugin_config_dir()  # 按顶层插件包名推导，返回绝对 Path 并创建目录
assert cfg.directory == directory
assert cfg.path == directory / "config.json"
cache_path = directory / "cache.json"
```

也可以显式调用 `get_plugin_config_dir("e5", create=False)`，只取路径、不创建目录或配置文件。目录位置不依赖进程工作目录。

升级时先停止旧进程。下次启动或执行 `python main.py --init-config`，会在读取配置前将旧的 [config/e5.json](config/e5.json)、[config/wm.json](config/wm.json)、[config/yiff.json](config/yiff.json) 移入各自目录，并统一命名为 config.json；E5 SQLite 与 WM 会话在对应服务初始化前迁入插件目录，数据文件名保持不变。旧默认数据路径会自动更新，显式自定义路径保持原有语义。

迁移可重复执行；新旧文件同时存在时会报错并保留两边，不覆盖已有文件。旧配置格式错误时也会保留原文件。主配置 [config.json](config.json) 不迁移。目录结构、字段和存储说明见 [配置说明](config/README.md)。

### 生命周期与 HTTP 回调

资源生命周期使用 `@startup` / `@shutdown`，支持无参数或一个 Application 参数的异步函数。每个插件自行负责客户端、后台任务与缓存：启动钩子创建资源，停止钩子取消并等待任务、关闭连接。框架不再专门管理某个业务插件的 HTTP 客户端；例如图站启动先创建 HTTP 客户端再启动帖子池，停止先停池再关客户端。

同类钩子按注册顺序执行，单个钩子异常会记录日志并继续执行其他钩子。重载和卸载使用严格停止模式：任何停止钩子失败，都会保留旧模块和注册表，中止后续卸载。此时部分资源可能已经停止，需排查日志后重试或重启，并不等同于恢复完整旧状态。

插件 HTTP 回调用 `@webhook_route("/your/callback")` 注册 GET 路由，仅 webhook 模式提供服务；回调接收 aiohttp Request，需要原始 Bot 时使用 `request.app[WEBHOOK_BOT_KEY]`，其中 `WEBHOOK_BOT_KEY` 从 `core` 导入。

### 热重载的边界

管理员执行 `/reload` 时，按以下顺序切换全部插件，而不是只重新执行处理器文件：

1. 暂停新的插件调用，等待正在执行的命令、按钮和 HTTP 回调结束；触发重载的当前任务不等待自己。
2. 执行旧插件的 `@shutdown`，关闭客户端、WebSocket 和后台任务。
3. 清空命令、按钮、生命周期、HTTP 回调与插件配置缓存，从 `sys.modules` 移除 `plugins` 及所有已导入的子模块，并清理对应插件字节码缓存。
4. 重新扫描顶层入口，导入插件及其私有工具，执行 `setup()`；若此前已启动，再执行新插件的 `@startup`，使用同一个 Application 重建资源。

重载期间被分发的新命令或按钮会提示稍后重试；插件 HTTP 回调返回 `503` 和 `Retry-After: 1`。Telegram 更新入口仍可入队。已有长请求会延迟重载，已排队的旧处理器不会在切换后继续执行。

`reload_all_plugins()` 与 `unload_all_plugins()` 都是异步接口，调用时需 `await`；仅导入配置、尚未启动的实例执行重载不会启动网络资源。重载不重新加载 `core`、共享 `utils` 或第三方依赖。导入失败会记录日志并跳过，启动钩子失败会记录日志，整体没有事务性回滚。

| 修改内容 | 生效方式 |
| --- | --- |
| 单文件插件、包内处理器和工具、插件配置 | `/reload`，包内模块和资源一起重建 |
| 主配置、core / utils 代码、第三方依赖 | 重启 |
| 已存在 HTTP 路由的处理函数 | `/reload`；新增或更改监听路径需要重启 |
| 图站 `site`、`pool.*`、后台补池标签、UA / browser | `/reload`，重新创建 HTTP 客户端、帖子池与搜索缓存 |
| WM 客户端配置、启动自动登录、预热开关 | `/reload`，重新执行启动逻辑，名称索引与缓存重新构建 |
| E5 定时器启停、间隔等配置 | `/reload`，重新创建调度器并从头等待下一轮周期 |

WM 会话文件、E5 SQLite 数据库和待完成 OAuth 绑定保存在插件配置目录或显式自定义路径，不随包卸载删除。WM 状态连接会关闭，需重新设置在线状态；图站换图记录、WM 候选选择与改价计划等内存状态会清空，旧按钮需重新发起命令。需要长期保留的数据应落盘，不应依赖旧模块单例。

## 验证与维护

安装运行依赖后即可使用标准库 `unittest`，不需要另装测试框架：

```bash
python -m unittest discover -s tests -v
python -m compileall -q core utils plugins tests main.py
```

测试覆盖主配置兼容、插件配置隔离、命令与按钮分发、文件句柄关闭、生命周期清理、webhook 请求与路由、E5 OAuth/SQLite，以及全部插件的初始化和热重载。插件加载测试在临时项目副本中运行并禁止网络连接；HTTP 测试仅启动本机临时监听。测试不需要真实 Bot Token、WM 账号或 Microsoft 凭据，不代表外部服务联调已经通过。

本地配置、WM 会话和 E5 SQLite 文件已加入 [.gitignore](.gitignore)，但忽略规则不等于加密。WM 会话包含 JWT，E5 数据库包含应用密钥和 refresh token；POSIX 权限设置为尽力而为，部署时应自行限制访问和保护备份。

许可证：[MIT](LICENSE)。
