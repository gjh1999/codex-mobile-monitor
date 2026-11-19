# 配置与数据位置

[文档索引](README.md) · [运行与维护](RUNBOOK.md)

## 配置和状态根目录

`monitor.ROOT` 统一决定应用配置和状态位置：

| 运行方式 | 根目录 |
|---|---|
| 源码 `desktop_app.py` | 内部模块 `monitor.py` 所在目录，即本项目根目录 |
| PyInstaller EXE | `sys.executable` 所在目录；通常为 `release/` |

源码运行和 EXE 都读各自根目录的 `local-config.json`。文件不存在时使用空配置和各调用点的默认值；配置须为有效 JSON，支持 UTF-8 BOM。PyInstaller 的临时资源目录 `_MEIPASS` 只用于打包资源，不保存持久状态。应用由 `desktop_app.py` 或桌面 EXE 启动，`monitor.py` 只提供进程内后台功能。

## 顶层配置键

[示例配置](../local-config.example.json) 只含占位内容，不填写真实凭据或个人会话 ID。所有自动接入和控制都是显式配置行为。

| 键 | 未提供时 | 用途 |
|---|---|---|
| `tasks` | `[]` | Codex 调度候选项；配置本身不授予线程所有权 |
| `managedTasks` | `[]` | 由服务创建与恢复的 Codex 新任务 |
| `autoAttachThreads` | `[]` | 启动时写入 Codex 接入列表的会话 ID |
| `claudeAutoAttachThreads` | `[]` | 启动时写入 Claude 接入列表的会话 ID |
| `autoApprovalRules` | `[]` | Codex 精确命令自动审批规则 |
| `streamIdentities` | `[]` | 钉钉 Stream 私聊身份白名单；空列表拒绝全部命令 |
| `desktopWallpaperMinutes` | `30` | 每隔多少分钟轮换桌面壁纸。填写 1–1440 的整数，填 0 固定使用 Mavericks 海浪。重启生效 |

服务循环会重新读取任务配置与审批规则；自动接入 seed、Stream 路由身份在启动时建立。为统一应用更改，修改后重启对应实例。已移出的任务意图保存在控制数据库，seed 不会覆盖已有的移出状态。

## 任务与自动审批

`tasks` 的条目包含 `threadId`、`prompt`、`interval`、`enabled`、`exclusiveOwner`，可用 `completed` 停止后续调度。`interval` 缺省为 3600 秒，显式值最小为 60 秒。只有启用、独占、本连接拥有的 Codex 线程才可能发送续跑；仍需实时 idle、active goal、无待审批、稳定版本及发送前持久预留。

`managedTasks` 用于创建本服务的新线程，不使用外部 `threadId` 声明所有权。条目必须填写 `name`、`objective`、`prompt`，并同时设置 `enabled: true`、`exclusiveOwner: true`。`cwd` 默认 `"."`，相对 `ROOT.parent` 解释，且必须是该工作区内已存在的目录。源码运行时这一工作区是 `To_Do_List`；EXE 位于 `release/` 时则是项目目录，移动 EXE 会改变此边界。

示例受管任务显式使用 600 秒周期，并默认禁用；这是示例值，代码默认仍为 3600 秒。目标或 prompt 的配置指纹变化会阻止同名恢复；创建结果不确定时保留记录，避免重复创建。普通任务注册不会将暂停、完成或受限目标重新激活。Codex 额度恢复由独立的持久等待流程处理：只对明确开启自动推进且有控制权的原任务，在官方额度重新确认允许后恢复 `usageLimited`，不重写目标、清零用量或提高预算。

界面接入的 Codex 任务默认开启“自动推进”，使用固定 3600 秒周期；“关闭自动推进”保留手动控制，“暂停”阻止调度并在有控制权时请求中断。没有 active goal 的 Codex 测试任务不会自动续跑。Claude 自动推进默认关闭，须显式开启；其本地定时记录、revision 去重和发送前预留保存在 `claude-task-control.sqlite3` 的 `auto_progress` 表。首次等待 3600 秒，忙碌、失去控制权或待审批时延后检查；暂停任务不发送，未知送达关闭开关并禁止自动重放。Claude 没有原生 goal 完成接口，完成任务后应手动关闭此开关。

`autoApprovalRules` 每项包含 `threadId`、`cwd` 和完整 `command`。只对本 Codex 连接拥有的任务做精确匹配，目录须位于 `ROOT.parent` 内，并经过命令限制检查。默认没有规则。文件变更、权限扩展和 Claude 工具申请走人工处理。

## 环境变量

| 变量 | 用途 |
|---|---|
| `DINGTALK_WEBHOOK` + `DINGTALK_SIGNING_SECRET` | 同时存在时启用签名 Webhook 出站通知 |
| `DINGTALK_CLIENT_ID` + `DINGTALK_CLIENT_SECRET` | 同时存在时启动可选 Stream 接收器 |
| `CLAUDE_CODE_PATH` | 指定本机 Claude CLI 路径，否则自动发现 CLI |
| `CLAUDE_CONFIG_DIR` | 指定 Claude 会话目录，否则使用 `~/.claude` |

环境变量由启动进程继承。另一个终端临时设置的值不会自动传给已经运行的服务。凭据只保存在本机私密环境中，不写入配置示例、日志或聊天。Stream 身份格式和验证步骤见 [钉钉配置](DINGTALK_SETUP.md)。

## 本地文件

以下文件位于当前实例的 `ROOT`，由程序按需生成：

| 文件 | 内容 |
|---|---|
| `local-config.json` | 当前桌面实例的私密配置 |
| `local-model-api.json` | 自动分组使用的接口地址、模型及由当前 Windows 用户保护的 API 密钥 |
| `lanxi-groups.sqlite3` | 案头任务、随记和待办的模型分组结果及手动分组 |
| `scheduler.sqlite3` | Codex 下次调度、退避、发送预留，以及 `quota_waits` 额度等待/恢复预留 |
| `managed.sqlite3` | Codex 受管任务注册与配置指纹 |
| `task-control.sqlite3`、`claude-task-control.sqlite3` | 分客户端的接入、暂停、托管意图和控制操作队列 |
| `replies.sqlite3`、`claude-replies.sqlite3` | 分客户端的本地消息与任务事件 |
| `notifications.sqlite3` | 主服务 Webhook 发送预留及结果 |
| `windows-notifications.sqlite3`、`claude-windows-notifications.sqlite3` | 分客户端的 Windows 提醒去重 |
| `claude-drafts.json` | 尚未发送首条消息的 Claude 测试任务草稿 |
| `*.log` | 启动方式或维护工具生成的日志，不保证每种模式都有 |

CLI 自身的记录另外保存：Claude 从 `CLAUDE_CONFIG_DIR` 或 `~/.claude` 读取会话及 live 元数据；Codex 历史标签读取 `USERPROFILE/.codex/sessions`。这些记录不属于应用 SQLite 状态目录。

私密配置、草稿、数据库及 sidecar、日志、`.venv/`、`build-exe/`、`release/` 已列入项目 `.gitignore`。备份或恢复状态应先退出相应实例并保留完整数据目录；不要只备份 EXE。
