# 本地配置与数据目录

插件通过 `core.plugin_config({...})` 声明默认值，首次导入时生成 [插件名/config.json](../README.md#配置目录与旧数据迁移)。基础工具插件没有独立配置，三个业务插件使用以下布局：

| 文件 | 用途 |
| --- | --- |
| [yiff/config.json](yiff/config.json) | 图站、默认标签、帖子池、媒体发送和浏览器 HTTP 配置 |
| [wm/config.json](wm/config.json) | Warframe Market API、账号、查价、限流、缓存与昵称配置 |
| [e5/config.json](e5/config.json) | E5 账户、Graph 定时活动、OAuth 回调与数据库配置 |
| [wm/wm_sessions.json](wm/wm_sessions.json) | WM 会话库，包含按 Telegram 用户 ID 保存的 JWT |
| [e5/e5_accounts.sqlite3](e5/e5_accounts.sqlite3) | E5 账户、应用密钥、refresh token 和待完成绑定 |

## 初始化与迁移

从项目根目录执行：

```bash
python main.py --init-config
```

该命令无需主配置或 Bot Token，不连接 Telegram、不执行启动钩子，但导入 E5 插件时会初始化 SQLite 数据库。WM 会话文件在实际保存会话时生成。

升级时先停止旧进程，再执行上面的命令或正常启动。读取插件配置前会将旧的 [e5.json](e5.json)、[wm.json](wm.json)、[yiff.json](yiff.json) 等文件移动到各自的插件子目录，并统一命名为 config.json。E5 数据库与 WM 会话文件也会在对应服务初始化前迁入子目录，文件名保持不变，旧默认路径配置随之更新；显式自定义的数据路径保留。

迁移可重复执行。新旧文件同时存在时会报错并保留两边，不覆盖已有数据；需先核对、备份并处理冲突文件后重试。SQLite 的 WAL、SHM、journal 附属文件一起迁移，已发布完整目标后才清理源文件；占用或文件操作失败时会中止该插件加载，保留可恢复的数据。旧配置格式错误时也会保留原文件。主配置仍是项目根目录的 [config.json](../config.json)，模板为 [config.example.json](../config.example.json)，只负责 Telegram、管理员和日志。`owner_ids` 默认空，需要自行填写管理员 ID。

## 修改与生效

配置文件缺少的字段会按默认值递归补齐，多余字段会保留；类型不匹配时尝试转换，失败则使用默认值。JSON 不支持行注释，说明文字可放在 `_comment` 字段中。

插件代码和配置通过 `/reload` 整包重载：先等当前调用结束，执行停止钩子，卸载包内全部已导入模块，再重新加载并执行启动钩子。因此图站站点/帖子池参数、WM 自动登录与预热开关、E5 定时器启停和间隔都可随重载生效。E5 调度器会重新开始等待周期，WM 状态连接需重新设置；会话、数据库和待完成绑定记录不会删除。

主配置、共享 `core` / `utils`、依赖以及新增或更改 HTTP 监听路径仍需重启。停止钩子失败时会保留旧模块并中止卸载，需排查日志后重试或重启。详细边界及所有命令见 [项目 README](../README.md#热重载的边界)。

包内工具与处理器默认共用顶层插件名的配置，例如 `plugins.e5.e5` 和 `plugins.e5.e5_renewal` 都对应 [e5/config.json](e5/config.json)。插件代码读取到的字典和列表为副本，应通过显式接口保存修改：

```python
from core import get_plugin_config_dir, plugin_config

cfg = plugin_config({"timeout": 20, "tags": []}, name="demo")
cfg.set("timeout", 30)
cfg.update(tags=["example"])
cfg.reload()

directory = get_plugin_config_dir("demo")  # 绝对 Path，目录不存在时创建
assert cfg.directory == directory
assert cfg.path == directory / "config.json"
cache_path = directory / "cache.json"
```

`get_plugin_config_dir()` 省略名称时，按调用者的顶层插件包名推导；它不会创建配置文件或加载插件处理器。传入 `create=False` 只取得路径，不创建目录。`plugin_config(auto_create=False)` 不生成或补写配置，但仍会移动已存在的旧配置文件。

## 浏览器 HTTP 配置

图站与 WM 插件的 `browser` 段可配置 HTTP 请求的浏览器指纹、Cookie、UA 和代理，例如：

```json
{
  "impersonate": "",
  "sync_ua": true,
  "user_agent": "",
  "cookies": {},
  "cookie": "",
  "cookie_domains": [],
  "headers": {},
  "proxy": "",
  "timeout": 20.0,
  "verify": true
}
```

这是 `browser` 子对象示例，不是整份插件配置。`impersonate` 为空时使用 httpx；非空时尝试使用依赖清单中的 `curl_cffi`。具体浏览器标识取决于安装版本，缺少该依赖时会回退 httpx；指纹与 Cookie 支持不保证站点接受请求。

`cookie` 字符串与 `cookies` 字典合并，同名时字符串覆盖。Cookie 按站点域和 `cookie_domains` 中显式指定的域注入。图站 `/fp` 仅展示 Cookie 名称。WM HTTP 与 WebSocket 超时使用 `api.timeout`，其 WebSocket 不复用这套 browser Cookie/代理配置。

## 存储与备份

默认目录由项目根路径定位，不依赖进程工作目录。自定义相对 WM `session_file` 仍基于进程当前工作目录，E5 `database_path` 的相对路径仍基于项目根目录。

本目录及子目录的 JSON 和 SQLite 文件已加入 Git 忽略规则，但它们仍是明文本地文件。请保护目录和备份，避免提交或分享密钥、JWT、refresh token。Windows 部署需自行设置访问 ACL。
