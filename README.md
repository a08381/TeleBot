# TeleBot

装饰器风格的 Telegram Bot 框架：插件只声明「我处理哪个指令」，
框架负责解析、分发、生命周期管理；插件里看不到任何 `import telegram`。

## 快速开始

```bash
pip install -r requirements.txt
cp config.example.json config.json   # 填好 t_token（owner_ids 建议改成你自己的 user id）
python main.py                       # 默认长轮询；webhook.enable=true 时走 webhook
```

过 Cloudflare 不需要额外起服务：装 `curl_cffi` 后填 `browser.impersonate`，
请求就会带上与真实浏览器一致的 TLS/HTTP2 指纹（详见下文「过 Cloudflare」）。

```bash
pip install curl_cffi
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
├── utils/                  通用工具层 —— 只依赖标准库 + httpx/curl_cffi，不依赖 core
│   ├── config.py           主配置加载（全部参数带默认值）
│   ├── browser_profile.py  浏览器指纹 + Cookie 配置、客户端工厂
│   ├── http_pool.py        取图客户端单例（带浏览器指纹）
│   └── posts_pool.py       帖子预热池
└── plugins/                业务插件 —— 只 import core / utils
    ├── ping.py  reload.py  whoami.py  wm.py  yiff.py
```

**两级配置**：Bot 本体与基础设施（Token、API 地址、主人 ID、日志、webhook）
在根目录 `config.json`；业务参数（e621/e926 站点、UA、标签、图池、限流，以及浏览器
指纹与 Cookie）在 `config/yiff.json`，由插件声明默认值后自动生成 ——
主配置里没有一行图站相关的内容。

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
`post_init` / `post_shutdown` 阶段统一调用（取图客户端之后启动、之前停止）：

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
因此**跨重载需要保留的单例要放 `utils/`**（`http_pool` / `posts_pool` 就是这么做的），
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
  "log_dir": "logs"
}
```

`telegram.host` 只在用自建 API / 反代时改，`webhook.enable=false` 走长轮询（默认）。
**不含任何图站参数**。没写的字段一律用代码默认值。

旧格式的顶层 `t_token` / `t_host` / `webhook` 仍能读，自动兜底（telegram 段优先），
迁移时不用一次性改完。

这些都是通用调参，**默认即可**，看不懂就不用动。

`config/yiff.json`（e621/e926 全部参数，运行时自动生成）：

| 字段 | 说明 |
| --- | --- |
| `site` | 站点地址，如 `https://e926.net` / `https://e621.net` |
| `default_tags` | 不带参数时的默认标签 |
| `user_agent` | 站点要求的合规 UA（e621 禁浏览器 UA，需带用户名） |
| `browser.impersonate` | **浏览器指纹**：`chrome124` / `chrome` / `firefox135` / `safari184`…，留空=不启用 |
| `browser.sync_ua` | 启用指纹时，用浏览器 UA 覆盖上面的 `user_agent`（默认 `true`） |
| `browser.cookies` / `browser.cookie` | 站点 Cookie：dict 或 `"a=1; b=2"` 字符串（登录态、手填的 cf_clearance） |
| `browser.cookie_domains` | 额外要发 Cookie 的域名（图片在 CDN 子域时填它） |
| `browser.timeout` | 单次请求超时（秒，默认 20） |
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

`user_agent` 与 `browser.*` 改完 `/reload` 也能生效：`http()` 发现站点或浏览器身份
变了会自动关掉旧客户端、按新参数重建。

### 过 Cloudflare：浏览器指纹 + Cookie

Cloudflare 看两样东西：**TLS/HTTP2 指纹** 与 **Cookie**。前者是硬门槛 ——
httpx 的握手形状一看就是脚本，还没轮到看 Cookie 就被 403 了。所以：

```bash
pip install curl_cffi
```

然后在 `config/yiff.json` 里配 `browser` 段：

```jsonc
"browser": {
  "impersonate": "chrome124",          // 留空=不启用指纹
  "sync_ua": true,                     // 用浏览器 UA 覆盖站点 UA（CF 会交叉验证两者）
  "cookies": { "login": "xxx" },       // 站点 Cookie，或 "cookie": "a=1; b=2"
  "cookie_domains": [],                // 额外要发 Cookie 的域名（CDN 子域）
  "headers": {},
  "timeout": 20.0
}
```

- 装没装 curl_cffi 都能跑：没装时自动退回 httpx 并打一条 warning，功能不受影响，
  只是没有指纹（`/fp` 会显示"未生效"）
- Cookie 只发给站点主域 + `cookie_domains`，不会泄漏给 CDN / 第三方域
- 改完 `/reload` 生效，图池不用重建 —— 它每次取图都现调 `http()`
- **注意冲突**：e621/e926 要求 UA 带用户名且不许用浏览器 UA，而过 CF 必须用浏览器
  UA，二选一（见 `config/README.md`）

> **为什么放在插件配置**：`site` / `user_agent` / 浏览器身份描述的都是
> 「访问哪个站、以什么身份」，同一个进程里可能有多个插件访问不同站点，
> 所以它们跟着插件走，主配置只留 Bot 本体那几项。

`/fp`（仅主人）可以随时查看当前指纹、Cookie 名与实际发出的 UA。

