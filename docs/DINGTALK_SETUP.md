# 钉钉配置

[文档索引](README.md) · [环境变量与状态目录](CONFIGURATION.md)

项目有两条独立通道：签名 **Webhook** 用于出站通知，应用机器人 **Stream** 用于接收有限指令和回复。分别配置其凭据；Webhook 本身不能接收文字回复。

当前源码已接入这两条可选路径。缺少对应环境变量时不启用该通道，桌面软件可继续运行。账号权限、实际联网和钉钉收到通知应按本机回执核验，历史安装记录不代表当前已经接通。

## Webhook 通知

在需要接收消息的钉钉群中创建允许使用的自定义机器人，启用签名校验。将 Webhook 和签名密钥配置到启动进程可继承的私密环境中：

- `DINGTALK_WEBHOOK`
- `DINGTALK_SIGNING_SECRET`

不把真实值写入示例文件、代码、日志或聊天。适配器接受官方 `https://oapi.dingtalk.com/robot/send` 目标，检查业务返回码，且不在错误中输出凭据。

在项目根目录运行检查，默认不发网络请求：

```powershell
& './.venv/Scripts/python.exe' './scripts/notify_cli.py'
```

缺少配置返回 `not_configured`；配置完整且格式有效返回 `configured_dry_run`。明确指定真实收件群并准备好接收测试消息后，才执行：

```powershell
& './.venv/Scripts/python.exe' './scripts/notify_cli.py' --send --event first-dingtalk-test
```

| 结果 | 含义 |
|---|---|
| `sent` | 钉钉业务接口确认接受，仍需在钉钉客户端检查是否收到通知 |
| `unknown_delivery` | 可能已送达但未获得确认，不应立即重复发送 |
| `rejected` | 业务接口拒绝，检查机器人配置及租户条件 |
| `duplicate` | 本实例已处理相同事件 |

主服务当前通过 Webhook 提醒待审批事项，并以 `notifications.sqlite3` 在发送前预留，跨重启避免重复外发；极端崩溃可能漏发，当前没有人工重发管理界面。独立 CLI 只做进程内去重，重复启动 CLI 不构成幂等发送。回复摘要等本机提醒由 Windows 通道处理。

## Stream 指令

在账号允许的范围内创建应用机器人、启用 Stream，并安装可选 SDK：

```powershell
& './.venv/Scripts/python.exe' -m pip install -r requirements-stream.txt
```

`requirements-lock.txt` 保留既有 Stream 依赖的锁定快照。实际 SDK 是否可用可通过项目回归测试中的离线回调测试检查；测试不建立真实 Stream 连接。

配置启动进程可继承的私密环境变量：

- `DINGTALK_CLIENT_ID`
- `DINGTALK_CLIENT_SECRET`

在当前实例的 `local-config.json` 填写 `streamIdentities`。以下只是格式占位：

```json
{
  "streamIdentities": [
    {
      "corpId": "YOUR_CORP_ID",
      "staffId": "YOUR_STAFF_ID",
      "conversationId": "YOUR_PRIVATE_CONVERSATION_ID"
    }
  ]
}
```

三项须全部匹配实际回调，且只接受私聊 `conversationType=1`；空列表拒绝全部命令。昵称和群管理员身份不能替代这三个标识。修改凭据或身份后重启实例；另一个终端临时设置的环境变量不会改变已运行的服务。

可用指令：

| 指令 | 行为 |
|---|---|
| `状态` | 返回 Codex 顶层状态摘要；当前没有客户端切换参数 |
| `待审批` | 列出共用审批队列中仍有效的请求 |
| `查看 <token>` | 展示具体请求，并生成 120 秒内有效的单次确认码 |
| `批准 <token> <确认码>` | 核验先前查看记录后批准该请求一次 |
| `拒绝 <token>` | 拒绝指定有效请求 |

相同消息 ID 在当前进程内去重；重启后不会持久保留消息去重缓存和确认码。详情过长或缺少具体操作内容时不能通过钉钉批准，应在桌面软件中完整核对。这里只处理本程序收到的请求，不操作其他桌面客户端的权限弹窗，也不支持任意终端命令、任务创建或暂停/恢复调度指令。

## 验收与排查

依次核验配置检查、Webhook 业务回执、钉钉收到提醒、Stream 私聊状态回复，再核验某个具体审批的查看和一次确认。Windows 通知调用、钉钉业务回执、SDK 离线回调分别只能证明对应环节。

没有响应时，检查桌面进程是否继承凭据、是否安装 SDK、机器人账号配置、回调中的三项身份与私聊类型。状态中的 `streamStatus=callback_received` 表示收到回调，不单独证明整条审批链路完成。Stream 是可选外部交互通道，桌面工作台仍是本机任务操作入口。
