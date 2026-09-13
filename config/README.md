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

插件配置管"这个插件自己的业务参数"，比如 yiff 插件的图站地址、UA、默认标签、
图池大小与限流、浏览器指纹与 Cookie，全在 `config/yiff.json` 里。

根目录 `config.json` 只留 Bot 本体与基础设施：Bot Token、API 地址、主人 ID、
日志、webhook —— 不含任何图站或浏览器相关的内容。

## 过 Cloudflare：浏览器指纹 + Cookie（yiff）

Cloudflare 看两样东西，缺一个都可能 403：

1. **TLS / HTTP2 指纹** —— httpx 的握手一看就是脚本，这是硬门槛
2. **Cookie** —— 登录态、或手填的 `cf_clearance`

先装依赖，再配 `yiff.json` 的 `browser` 段：

```bash
pip install curl_cffi
```

```jsonc
"browser": {
  "impersonate": "chrome124",   // 留空=不启用指纹；可选 chrome/chrome124/firefox135/safari184…
  "sync_ua": true,              // 用浏览器 UA 覆盖站点 UA（CF 会拿 UA 与指纹交叉验证）
  "user_agent": "",             // 手动指定 UA 时优先（自己保证与指纹一致）
  "cookies": {"cf_clearance": "xxx"},  // 站点 Cookie（登录态…）
  "cookie": "",                 // 或直接贴字符串 "a=1; b=2"（与 cookies 等价）
  "cookie_domains": [],         // 额外要发 Cookie 的域名（图片在 CDN 子域时填它）
  "headers": {},                // 附加请求头
  "proxy": "", "timeout": 20.0, "verify": true
}
```

要点：

- **没装 curl_cffi 不会报错**：自动退回 httpx，只是没有指纹 —— 启动日志有提示，
  `/fp` 也会显示"未生效"。
- UA 有个绕不开的冲突：e621/e926 要求"UA 带用户名、且不许用浏览器 UA"，而过 CF
  必须用浏览器 UA。二选一 —— 站点没拦你就关掉 `impersonate` 守站点规矩；
  已经被 CF 挡住就开指纹，牺牲站点的 UA 合规。
- Cookie 只发给站点主域（+ `cookie_domains`），不会泄漏给 CDN / 第三方域。
- 改完 `/reload` 生效：`http()` 发现浏览器身份变了会自动重建客户端，图池不用重建。
- 主人发 `/fp` 可查看当前指纹、Cookie 名（不显示值）与实际发出的 UA。
