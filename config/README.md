# 插件配置目录

`config/<插件名>.json` 由插件自己在首次加载时生成 —— 插件在 `plugins/xxx.py` 里
调用 `core.plugin_config({...默认值...})`，框架就会在这里落一份 JSON。

- 想改插件行为：直接编辑对应的 json，然后 `/reload`（或重启）生效
- 想加新配置项：改插件代码里的默认值，缺的字段会被自动补进文件
- 这些 json 已加入 .gitignore，不会进版本库（可能含 token 之类的敏感值）

不需要启动机器人也能生成：

```bash
python main.py --init-config
```

插件配置管"这个插件自己的业务参数"，比如 yiff 插件的图站地址、默认标签、
图池大小与限流，全在 `config/yiff.json` 里。

根目录 `config.json` 只留 Bot 本体与基础设施：Bot Token、API 地址、主人 ID、
日志、webhook，以及 FlareSolverr **客户端自身**的参数（地址、超时、重试、并发）。

FlareSolverr 里跟站点绑定的三项不算基础设施，它们描述"访问哪个站"，由插件声明：
`user_agent` 在 `yiff.json` 里；`entry_url` 默认就等于 `site`、`session_name` 默认取
站点域名，都不用配（确有需要时手动往 `yiff.json` 的 flaresolverr 段加这两个键即可）。

## 取图通道开关（yiff）

`yiff.json` 的 `flaresolverr.enabled` 决定取图走哪条通道：

- `true`：走 FlareSolverr 解 Cloudflare 挑战，带 `cf_clearance` 访问（默认）
- `false`：纯直连，普通 httpx 请求只带 UA，不连 FlareSolverr

改文件后 `/reload` 生效；也可以让主人发 `/fs on`、`/fs off` 运行时切换（会同时
写回这个文件），`/fs` 不带参数查看当前通道状态。

## 浏览器指纹与自定义 Cookie（yiff）

`cf_clearance` 只证明"有浏览器解过挑战"，Cloudflare 同时还看 TLS / HTTP2 指纹：
httpx 的握手形状一看就是脚本，光有 cookie 照样 403。想让请求真正"像浏览器"，
配 `yiff.json` 的 `browser` 段：

```jsonc
"browser": {
  "impersonate": "chrome124",   // 留空=不启用指纹；可选 chrome/chrome124/firefox135/safari184…
  "sync_ua": true,              // 把指纹 UA 同步给 FlareSolverr 与请求头（cf_clearance 与 UA 绑定，必须一致）
  "user_agent": "",             // 手动指定 UA 时优先（自己保证与指纹一致）
  "cookies": {"login": "xxx"},  // 站点 Cookie（登录态、手填的 cf_clearance…）
  "cookie": "",                 // 或直接贴字符串 "a=1; b=2"（与 cookies 等价）
  "cookie_domains": [],         // 额外要发 Cookie 的域名（图片在 CDN 子域时填它）
  "headers": {},                // 附加请求头
  "proxy": "", "verify": true
}
```

要点：

- 依赖 `curl_cffi`（`pip install curl_cffi`）。**没装不会报错**，自动退回 httpx，
  只是没有指纹 —— 日志里会有提示，`/fs` 也能看到"未生效"。
- `impersonate` 一开，UA 会自动换成对应浏览器的 UA，并同步给无头浏览器解挑战用。
  e621/e926 这类站要求"UA 带用户名、且不许用浏览器 UA"，与 CF 的要求冲突，
  二选一：要么关掉 `impersonate` 守站点规矩，要么开指纹 + `sync_ua` 先过 CF。
- Cookie 只发给站点主域（+ `cookie_domains`），不会泄漏给 CDN / 第三方域；
  走 FlareSolverr 时解出的 `cf_clearance` 会自动追加到 `cookies` 之上（同名覆盖）。
