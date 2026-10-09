# GitHub Action 部署（过时）

> ⚠️ **此方式已过时，不推荐。** 保留文档是为了有需要的老用户参考。

## 当前 Fork 的部署注记

本 Fork 保留 `.github/workflows/schedule.yml`：每天北京时间 **09:00** 自动执行，
使用 `user-data` Environment。GitHub 的定时触发可能延迟。

- `TASKS`、消息模板、代理和超时等配置读取该 Environment 的 Variables。
- 当前账号的 Cookie 只通过 Secret `COOKIES_35503009597` 显式映射，
  不批量导出所有 Secrets，不生成包含 Cookie 的 `.env`。`TASKS` 中对应账号的
  `unique_id` 必须与这个 Secret 后缀一致；增加账号时需同时增加 workflow 的显式映射。
- 手动触发的 `validate_only` 默认为 `true`。此模式只校验配置、安装依赖及启动
  应用实际使用的 CloakBrowser；**不会登录抖音或发送消息，也不验证 Cookie 登录状态**。
  需要执行发送任务时取消勾选；定时触发会正常执行任务。
  勾选 `test_once` 时，本轮最多向一个精确匹配的好友试发一条消息。
- 浏览器版本和 SHA-256 校验值与 `Dockerfile` 一致；下载并校验发布包后复制整个
  Chromium 目录，以保留 `icudtl.dat`、`.pak`、`locales` 等必需文件。
- 同步新版上游后，发送入口改为抖音网页版 `www.douyin.com/chat` 的 IM 会话，
  增加 HTTP 发送接口回执和 DOM 消息状态检查。日志中的 `via` 区分 `http`、
  `http+dom`、`dom`；HTTP 成功回执表示服务端接受，只有 DOM 变化时不能确认
  服务端接受。两者都不等于接收方已经收到或阅读。`validate_only` 不会触发发送。
  当前 Fork 仅把 HTTP 成功回执计为发送成功；DOM 变化单独记录为未确认。
  回执不明时不自动重发，避免重复消息。发送失败、查找未完成或缺少目标时，
  普通完整任务返回失败状态，Actions 不再显示假成功。
  好友仅按归一化后的备注、昵称、抖音号或 uid 精确匹配，排除群聊和名字子串。
  扫描超时仅计算查找耗时，不包含发送及等待回执的时间。

新版的时间配置单位为**秒**：`BROWSER_ACTION_TIMEOUT` 默认 `120`，
`FRIEND_LIST_WAIT_TIME` 在本 Fork 默认 `2`，两者支持小数。
`IM_SCAN_TIMEOUT`、`IM_READY_TIMEOUT` 默认 `120`，必须为正整数秒；
`IM_MAX_STEPS` 默认 `200`，表示最大扫描步数。`TASK_RETRY_TIMES` 为正整数次数。
旧的 `BROWSER_TIMEOUT` 已不再使用；旧 `FRIEND_LIST_WAIT_TIME=2000`（毫秒）应改为
`FRIEND_LIST_WAIT_TIME=2`（秒），避免扫描时等待过久。

运行日志保留 7 天。部署限制及代理注意事项仍按下文评估。

## 为什么过时

1. **GitHub 严格的 Action 审查**：公共仓库的定时任务容易被限制甚至禁用。
2. **出口 IP 每次运行都变**：抖音校验登录 Cookie 的出口 IP，IP 漂移会导致登录态掉，需要额外配固定代理。

## 如果要继续用（私有仓库方案）

要绕开 GitHub 对公共仓库 Action 的严格检查，需要把代码搬到私有仓库：

1. 把仓库 clone 到本地
2. 在 GitHub 上新建一个**私有**仓库
3. 把代码推送到私有仓库
4. 在私有仓库里启用 Action、配置定时任务

私有仓库的 Action 限制相对宽松，但**出口 IP 漂移的问题依然存在**，仍需自备固定代理。

## 关于出口 IP

Action 每次运行的 runner 不同，出口 IP 每次都变。抖音登录态绑定出口 IP，所以：

1. 需要一个固定出口的 HTTP 代理
2. 把代理地址写进 `.env` 的 `PROXY_ADDRESS`
3. 抓 Cookie 时也要走同一个代理出口

详细原理见 [Cookie 与出口 IP](guide/02-cookie与出口IP.md)。

## 历史部署流程（供参考）

老用户如需按原流程配置，可参考仓库根目录的历史 `docs/Action部署说明.md`（如已删除则看 git 历史）。核心步骤：

1. Fork 仓库
2. 创建名为 `user-data` 的 Environment
3. 在该 Environment 下配置 Variables 和 Secrets（逐条粘贴配置生成器产出）
4. 手动触发一次验证

> 如果你能自备服务器，请直接改用 [服务器 Docker](docker.md)，省去代理和私有仓库的折腾。
