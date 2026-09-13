# TeleBot

装饰器风格的 Telegram Bot 框架：插件只声明「我处理哪个指令」，
框架负责解析、分发、生命周期管理；插件里看不到任何 `import telegram`。

## 快速开始

```bash
pip install -r requirements.txt
cp config.example.json config.json   # 填好 t_token（owner_ids 建议改成你自己的 user id）
python main.py                       # 默认长轮询；webhook.enable=true 时走 webhook
```

FlareSolverr 不是 pip 依赖，需要单独起：

```bash
docker run -d -p 8191:8191 flaresolverr/flaresolverr
```

## 项目结构

```
TeleBot/
├── main.py                 入口：python main.py
├── requirements.txt
├── config.json             主配置（自己创建，不入库）
├── config.example.json     主配置模板
├── config/                 插件配置目录 —— 每个插件一份 json，插件自己生成
│   ├── yiff.json           图站（e621/e926）相关参数全在这里
│   └── ping.json  reload.json  whoami.json  wm.json
├── core/                   框架层 —— 唯一 import python-telegram-bot 的地方
│   ├── bot.py              Application 构建与启动（生命周期钩子）
│   ├── dispatcher.py       指令/按钮注册表 + 解析 + 分发
│   ├── loader.py           插件扫描、加载、热重载
│   ├── context.py          Context / User / Chat / Message / CallbackQuery 门面
│   ├── keyboard.py         Keyboard：链式构造内联键盘
│   ├── plugin_config.py    插件私有配置（config/<插件名>.json）
│   ├── errors.py           BotError（对 SDK 异常的统一包装）
│   └── logging_setup.py    日志（控制台 + 按天切分）
├── utils/                  通用工具层 —— 只依赖标准库 + httpx，不依赖 core
│   ├── config.py           主配置加载（全部参数带默认值）
│   ├── async_flaresolverr.py  FlareSolverr 异步客户端
│   ├── fs_pool.py          取图客户端单例（FlareSolverr / 直连）+ 后台保活
│   └── posts_pool.py       帖子预热池
└── plugins/                业务插件 —— 只 import core / utils
    ├── ping.py  reload.py  whoami.py  wm.py  yiff.py
```

**两级配置**：Bot 本体与基础设施（Token、API 地址、主人 ID、日志、webhook、FlareSolverr）
在根目录 `config.json`；业务参数（e621/e926 站点、标签、图池、限流等）在 `config/yiff.json`，
由插件声明默认值后自动生成 —— 主配置里没有一行图站相关的内容。

依赖方向单向：`plugins → core → utils`，utils 不反向依赖任何东西。

## 写一个插件

在 `plugins/` 下新建 `.py` 文件即可，启动自动加载，无需注册。

```python
from core import Context, Keyboard, listener, button

@listener("hello")                      # 处理 /hello
async def hello(ctx: Context, *args, **kwargs):
    await ctx.reply_text(f"hi, {ctx.user.full_name}")

@button("demo:")                        # 处理 callback_data 以 demo: 开头的按钮
async def demo(ctx: Context, payload: str):
    await ctx.answer_query(f"你点了 {payload}")
```

指令支持 `/cmd arg1 k=v` 形式：`arg1` 进 `*args`，`k=v` 进 `**kwargs`；
`/cmd@BotName` 只在 @ 了自己的时候才响应。

### Context 能做什么

| 分类 | API |
| --- | --- |
| 读信息 | `ctx.user`（`.id` `.username` `.full_name` `.is_owner`）· `ctx.chat` · `ctx.message`（`.id` `.date` `.text`）· `ctx.query` · `ctx.args` / `ctx.kwargs` |
| 发送 | `reply_text` · `reply_photo` · `reply_document` · `send_text` · `send_photo` · `send_document` |
| 编辑 | `edit_photo` · `edit_document` |
| 回调 | `answer_query(text=None, show_alert=False)` |
| 逃生舱 | `ctx.bot` / `ctx.update`（拿到原始 PTB 对象，一般不需要） |

发送失败统一抛 `core.BotError`（插件不需要知道底层 SDK）。
按钮用 `Keyboard()` 拼：`.url(text, url)` / `.callback(text, data)` / `.row()` 换行。

### 插件自己的资源（startup / shutdown 钩子）

插件要随进程启停的资源（连接池、后台任务），用钩子声明，框架在
`post_init` / `post_shutdown` 阶段统一调用（FlareSolverr 之后启动、之前停止）：

```python
from core import shutdown, startup
from utils.posts_pool import PoolSettings, pool_start, pool_stop

@startup
async def _start_pool(application=None):     # 参数可以省略
    await pool_start(PoolSettings.from_mapping(cfg.as_dict()))

@shutdown
async def _stop_pool(application=None):
    await pool_stop()
```

注意：**实例要放 `utils/`（模块级单例），别放插件里** —— `/reload` 会重新导入
`plugins.*`，写在插件模块里的状态热重载一次就没了。钩子本身只在进程启动时跑一次。

### 插件自己的配置

插件在代码里声明默认值，框架首次加载时自动在 `config/<插件名>.json` 生成文件，
**不需要往根目录 config.json 里加字段**：

```python
from core import plugin_config

cfg = plugin_config({          # 插件名自动取模块名：plugins/demo.py -> config/demo.json
    "timeout": 10,
    "vip_ids": [],
})

cfg.get("timeout")     # 读，也可用 cfg.timeout / cfg["timeout"]
cfg.set("timeout", 30) # 改并立即落盘
cfg.update(vip_ids=[1, 2])
cfg.reload()           # 重新读盘（/reload 时会自动做）
cfg.reset()            # 恢复默认值
```

文件行为：

| 情况 | 处理 |
| --- | --- |
| 文件不存在 | 用默认值生成一份 |
| 文件缺字段 | 补上默认值并写回 |
| 文件多出字段 | 保留，不删 |
| 类型不对（如填了 `"3"` 而默认是 `3`） | 按默认值类型强转，转不过来回退默认值 |

不想启动机器人就想把配置生成出来：`python main.py --init-config`。
`config/*.json` 已在 .gitignore 里，不会进版本库。

## 热重载

`/reload`（仅 `owner_ids` 里的用户可用）会清空注册表、卸载 `plugins.*` 模块并重新导入。
因此**跨重载需要保留的单例要放 `utils/`**（`fs_pool` / `posts_pool` 就是这么做的），
插件模块级的缓存会随重载一起重置。

## 配置

根目录 `config.json`（主配置，模板见 `config.example.json`）：

```json
{
  "telegram": {
    "token": "123456:ABC...",
    "host": "https://api.telegram.org",
    "webhook": { "enable": false, "url": "", "port": 7755, "path": "/" }
  },
  "owner_ids": [535840409],
  "log_level": "INFO",
  "log_dir": "logs",
  "flaresolverr": { ... }
}
```

`telegram.host` 只在用自建 API / 反代时改，`webhook.enable=false` 走长轮询（默认）。
**不含任何图站参数**。没写的字段一律用代码默认值。

旧格式的顶层 `t_token` / `t_host` / `webhook` 仍能读，自动兜底（telegram 段优先），
迁移时不用一次性改完。

主配置里的 `flaresolverr` 段只保留「客户端本身怎么工作」的参数，
站点相关的三项由插件提供（见下）：

| 字段 | 含义 | 什么时候才需要改 |
| --- | --- | --- |
| `url` | FlareSolverr 服务地址 | 服务不在本机 8191 时 |
| `fs_timeout` | 调 FlareSolverr 的超时 | 挑战特别慢、频繁超时时调大（默认 120s，不建议调小） |
| `request_timeout` | 拿到 cookie 后直连目标站的超时 | 目标站响应慢时调大 |
| `renew_margin` | cookie 还剩多少秒过期时提前续 | 想更早/更晚续期时 |
| `max_retries` | 遇到 403/503 判定为挑战后重解重试几次 | 想让它多试几次时调大 |
| `concurrency` | 直连目标站的并发上限 | 想放宽/收紧并发时 |
| `refresh_interval` | 后台保活多久检查一次 cookie | 想让续期检查更频繁时 |

这些都是通用调参，**默认即可**，看不懂就不用动。

`config/yiff.json`（e621/e926 全部参数，运行时自动生成）：

| 字段 | 说明 |
| --- | --- |
| `site` | 站点地址，如 `https://e926.net` / `https://e621.net` |
| `default_tags` | 不带参数时的默认标签 |
| `flaresolverr.enabled` | **取图通道开关**：`true` 走 FlareSolverr 解挑战，`false` 纯直连（默认 `true`） |
| `flaresolverr.user_agent` | 无头浏览器用的 UA（e621 禁浏览器 UA，需填合规 UA）；直连时也是它 |
| `pool.size` | 预热池容量 |
| `pool.low_water` | 低于此数量开始补货 |
| `pool.refill_interval` | 补货检查间隔（秒） |
| `pool.min_interval` | 请求最小间隔，e621 限 2 req/s，建议 >=1.1 |
| `pool.cache_ttl` | 搜索结果缓存时长（秒） |
| `upload_limit_mb` | 自下载后上传的体积上限 |
| `photo_exts` | 走 photo（而非 document）的扩展名 |
| `enable_next_button` | 是否挂「换一张」按钮 |
| `caption` | 图片说明，支持 `{id}` |

`pool` 段只填部分字段也没关系，缺的会按默认值补齐（嵌套配置同样支持）。

其它插件的 `config/<插件名>.json`：

| 插件 | 可配项 |
| --- | --- |
| ping | `precision` 小数位 · `suffix` 单位后缀 |
| whoami | `template` 文案模板（`{id}` `{username}` `{full_name}`）· `show_full_name` |
| wm | `prompt` 提示语 · `per_row` 每行按钮数 · `options` 按钮列表 |
| reload | `extra_owner_ids` 额外管理员 · `message` 回执文案 |

改完插件配置执行 `/reload` 即可生效；唯一例外是 `site` 与 `pool.*` ——
图池在进程启动时按这些参数建好了，改它们需要重启进程。

`flaresolverr.user_agent` 改完 `/reload` 也能生效：`fs()` 发现站点信息变了会自动
关掉旧 session、按新参数重建客户端。

### 取图通道：FlareSolverr / 直连

`flaresolverr.enabled` 决定 yiff 用哪条通道取图，两种切法都行：

- 改 `config/yiff.json` 里的 `enabled`，然后 `/reload`
- 主人直接发 `/fs on`（走 FlareSolverr）、`/fs off`（直连），**运行时立即生效**，
  同时把开关写回 `config/yiff.json`，重启后保持；`/fs` 不带参数查看当前通道与
  cf_clearance 状态

通道切换只重建取图客户端，**图池不用重建** —— 它每次取图都现调 `fs()`，
下一个请求自动走新通道。切走 FlareSolverr 时会 destroy 浏览器 session，
切回来则在后台预热（解挑战可能要几十秒，不阻塞指令）。

`entry_url`（触发挑战的入口页）默认就等于 `site`，`session_name` 默认取站点域名，
**都不需要配**。只有 UA 必须自己填 —— 它是站点对爬虫的合规要求，没法从地址推出来。

> **为什么这么分**：`site` / `entry_url` / `session_name` / `user_agent` 描述的是
> 「要访问哪个站」——同一个 FlareSolverr 服务可能先后被不同插件用来访问不同站点，
> 所以它们跟着插件走；超时、重试、并发是客户端自身行为，跟站点无关，留在主配置。
