# 实施与验收记录

> 历史验收日志。各条结论只描述记录当时的版本和环境；旧文件名与路径的迁移见 [整理记录](REORGANIZATION_2026-11-14.md)，当前操作见 [运行与维护](../RUNBOOK.md)。

## 2026-11-18 澜汐顶部玻璃与动效复核

- 对照通过的 `docs/design-preview/workspace-hover.png` 与首版实机截图，校正顶部胶囊位置及分段宽度、右侧计数卡的宽度和对齐。计数卡设为三个 60 像素等宽栏，分隔线为 1×35 像素。顶部胶囊与滑块直接采样柔化壁纸。诗句换为 34 pt 粗宋体并加 0.85 像素描边，客户端标签用 11 pt 粗体。
- 按 [Apple Design skill](https://github.com/emilkowalski/skills/blob/main/skills/apple-design/SKILL.md) 的原则实现按下反馈、跟随指针、速度交接和中途改向。滑块使用临界阻尼，角频率 25 s⁻¹，16 毫秒的计时器步进，减弱动态效果分支立即停靠。未测量实际刷新率或输入到屏幕的延迟。
- Windows 11 x64、Python 3.14.7、PySide6 6.12.0。设置 `QT_QPA_PLATFORM=windows`，运行 `python tests/smoke_desktop.py --output build-exe/verification/lanxi-glass-header-final`。最终实机截图复核三栏、描边和玻璃层，合成 Qt 输入验证按下、64 像素拖动、反向时保留位置与速度、松开停靠和关闭动画。第一轮截图的宋体细笔画偏轻，加粗轮廓后复测。
- 在同一虚拟环境运行 `python -m unittest discover -s tests -t . -p 'test_*.py' -q`，119 项中 118 项通过、1 项跳过，耗时 11.586 秒。
- 使用 PyInstaller 6.22.3，通过 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/build-exe.ps1 -AppName Lanxi-Glass-R2` 生成 `release/Lanxi-Glass-R2.exe`，54,087,019 字节，首版玻璃包为 54,082,682 字节。验收包含源码原生窗口及 EXE 归档。旧版实例仍占用单实例锁，未做新 EXE 的独立启动。

## 2026-11-18 澜汐毛玻璃工作台

- 以确认的桌面预览和旧版原生窗口为基线。保留 Qt Widgets，任务列表与对谈面板从同一张壁纸采样并使用相同半透明底色。新增居中客户端滑块、定时壁纸轮换、悬停操作、独立审批窗、就地改名与 VS Code「启卷」。点击任务后由本地控制器先接入目标，再断开同端其他任务。已有运行轮次不因切换而中断，草稿按客户端和任务保留。
- 环境为 Windows 11 x64、Python 3.14.7、PySide6 6.12.0 和 PyInstaller 6.22.3。壁纸输入为现有 Mavericks 及 512 Pixels 档案的 Big Sur 白昼和 Catalina 夜景，后两张缩放至 2560 像素宽。默认 30 分钟轮换，启用系统动画时 600 毫秒渐变。壁纸实际长期轮换与跨显示器性能未测。
- 将 `QT_QPA_PLATFORM` 设为 `windows` 后运行 `python tests/smoke_desktop.py --output build-exe/verification/lanxi-glass-acceptance`。首次悬停验证的鼠标模拟未可靠触发进入事件，改用显式 Enter/Leave 事件验证淡入淡出，保留真实 Qt 鼠标点击和键盘测试。中间版本的半透明消息底色因 Qt HTML 导入重复填充文字背景而出现条纹，清理后复测截图已确认。最终用虚构双端会话验证了两块玻璃采样、列表点击与草稿隔离、双击改名、VS Code 参数、轮换计时器、审批来源、托盘弹窗及 1020×640 布局。
- 在同一虚拟环境运行 `python -m unittest discover -s tests -t . -p 'test_*.py' -q`。相比 118 项基线新增 1 项任务切换回归，覆盖接入失败、快速选择、旧消息取消与接入状态保存。119 项中通过 118 项、跳过 1 项，耗时 11.109 秒。
- 用 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/build-exe.ps1 -AppName Lanxi-Glass` 生成并列的 `release/Lanxi-Glass.exe`，体积 54,082,682 字节，旧包为 51,793,094 字节。打包日志位于同一验收目录。EXE 归档检查确认 Windows Qt 插件、新桌面模块、图标及三张壁纸均已内置。旧版仍在运行，因此以源码原生窗口和打包资源做验收。真实账号审批和从新版点击启动真实 VS Code 窗口未测。

## 2026-11-18 澜汐 Windows 原生交付

- 目的为让 Windows 用户双击进入独立桌面窗口。沿用旧版 Qt Widgets 的本机任务控制、托盘和 SQLite 状态，在原生窗口加入澜汐品牌、诗句与 Mavericks 海浪壁纸。
- 环境为 Windows 11 x64、Python 3.14.7、PySide6 6.12.0、PyInstaller 6.22.3。运行 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/build-exe.ps1` 生成 `release/Lanxi.exe`，使用单文件 GUI 模式，体积 51,793,094 字节。归档内确认含 Qt Widgets、Windows 平台插件和壁纸。旧版桌面 EXE 体积未测。
- 首次探索构建了浏览器工作台启动包。需求明确为原生桌面后，转为构建 Qt 入口。首次隔离启动已产生 SQLite 状态，隐藏窗口用 PowerShell 的主窗口标题检查未能识别。改用按进程 ID 枚举 Windows 顶层窗口，隔离包在 1.22 秒内检测到“澜汐”窗口及任务控制数据库。现场目录为 `build-exe/verification/packaged-native-143342493/`。
- Qt offscreen smoke 通过，但离屏截图缺少本机中文字形。首次改用 Windows 平台插件执行时，测试仍查找旧品牌。更新断言并加上壁纸加载检查后，用 `QT_QPA_PLATFORM=windows` 运行 `python tests/smoke_desktop.py --output build-exe/verification/lanxi-native-win-final` 通过，已查看原生截图。输入为虚构 Codex 与 Claude 会话，覆盖任务控制、草稿、中文、按钮动画、紧凑窗口和托盘恢复。
- 在项目虚拟环境执行 `python -m unittest discover -s tests -t . -p 'test_*.py' -q`，118 项运行，117 项通过，1 项跳过，耗时 11.243 秒。仍有既有 SQLite 与 TestClient 警告。
- 打包和隔离启动已验证。真实模型收发、实际工具审批、Windows 其他机器兼容性和复杂转场性能需另测。当前交付使用现有 Qt Widgets 的按钮动画，尚未引入 Qt Quick / QML。

## 2026-11-12：钉钉路线冻结与 P0 核验

已完成的具体动作：

- 写入 `钉钉监控与长程任务守护_实施计划书.md`，以钉钉为默认路线，取消继续扩张 Bark/飞书范围。
- 核对现有 `monitor.py`、`automation.py`、页面、CLI 0.167.0 及已生成 app-server schema。
- 核对钉钉官方 Stream Python SDK，确认存在机器人消息、事件和卡片回调接入；当前用户租户配置尚未验证。
- 执行本地既有测试，3 项通过：审批过期及幂等、未知/notLoaded 不唤醒、默认只演练。

这 3 项测试不是完整计划验收。尚未证明钉钉签名、手机收到通知、Stream 发送者鉴权、真实审批回传、持久调度和重启恢复。

## 已交给实现阶段的问题

1. 额度接口拒绝应独立退避，不得为重试额度而销毁承载任务与待审批的连接。
2. RPC 输入写入需串行化，通知、请求、响应分别处理，不能吞掉所有异常后丢失状态。
3. `serverRequest/resolved`、断连和过期需要撤销本地审批，并验证迟到点击不能响应到错误连接。
4. `notLoaded` 不代表桌面客户端空闲；托管会话注册、独占所有权和持久去重完成前不启用真实续跑。
5. 先做钉钉通知的本地 mock 与凭据配置说明；没有用户机器人配置时禁止实际外发。

## 阶段状态

| 阶段 | 状态 |
|---|---|
| P0 现状审计 | 已完成第一轮，问题已登记 |
| P1 钉钉通知 | 实现中，未真实接通 |
| P2 稳定连接 | 修复中，未完成全生命周期验证 |
| P3 任务守护 | 有保守原型，持久调度和真实托管续跑待验收 |
| P4 审批闭环 | 有原型，手机实际审批待验收 |
| P5 双向 Stream | 待应用凭据和身份配置；可先做离线测试 |
| P6 常驻交付 | 未完成 |

用户明确要求不控制桌面或浏览器。所有后续实施只通过文件、命令行和官方协议进行。

## P1 第一批实际交付

新增 `channels_dingtalk.py`：签名文本 Webhook、HTTPS 官方目标限制、业务返回码检查、进程内去重、明确限流时退避、网络结果不确定时不自动重发。所有异常结果为脱敏状态，不输出 Webhook 或签名密钥。

新增 `test_dingtalk_channel.py`，离线 transport 验证签名、消息格式、业务拒绝、去重、错误目标和网络不确定性。与原有测试合计 7 项通过。没有真实钉钉外发，也尚未完成跨重启事件队列，因此 P1 只算部分完成。

新增 `notify_cli.py` 与 `钉钉本机配置步骤.md`。默认只检查环境配置，不发网络请求；真实测试需显式 send，且由用户本机设置收件群的凭据。主循环集成、Stream、持久守护与真实审批仍未完成。

独立实现任务最近处于 waitingOnApproval，不能把其活跃状态当作代码进展。根任务已继续实现不需要外部安装的离线适配与测试，并通知实现任务文件所有权，避免冲突。

## 2026-11-12：根任务接入主服务（替代前面的旧原型说明）

已完成：

- 主循环移除 Bark，接入签名钉钉 Webhook；通知发送前预留 SQLite 记录，跨重启阻止重复外发。
- RPC 输入串行化、响应分发、断连唤醒等待请求、serverRequest/resolved 处理；额度查询失败不销毁连接。
- 调度持久保存间隔、失败退避和发送前预留；增加活动 goal 和待审批检查。未加载、未知和非独占线程继续阻止唤醒。
- 手机网页显示连接、额度、任务、调度和具体审批信息；接口令牌认证、默认只监听回环地址。
- 可选 Stream 指令路由：企业/员工/私聊会话三重白名单，重复消息抑制，查看操作后一次性确认码，120 秒到期。SDK 适配器依据官方代码编写，尚未安装和联网验收。
- 实际执行 21 项离线测试全部通过；包含本地 HTTP 请求测试，不调用钉钉、不启动模型任务。

尚未完成：

1. 用户钉钉应用/机器人的账号配置及 iPhone 收发实测；凭据必须在本机私密设置，不放聊天。
2. 托管线程注册、所有权恢复及真实长程任务续跑。当前 owned_threads 为空，真实唤醒禁用；这是未完成项，不能称为守护已投产。
3. 文件变更完整预览，及完整 app-server 连接端到端审批。
4. Stream SDK 真实连接、断线恢复和进程退出验收。
5. Windows 开机常驻/重启恢复安装；当前仅手动进程方式。

本轮没有控制桌面、浏览器或替用户点击任何权限提示。测试不代表真实外部服务已接通。

## 本轮最终现场状态（以后面的记录为准）

- 26 项测试通过，新增托管任务创建/恢复、不确定创建禁止重试、目标修改阻止恢复及真实钉钉 SDK 离线回调测试。
- 官方 dingtalk-stream 0.24.3 已安装在本项目独立 .venv，依赖版本保存到 requirements-lock.txt，没有修改医学训练虚拟环境。
- ManagedTasks 支持仅创建本服务新线程、SQLite 注册记录、重启恢复和固定目标校验；没有启用任何托管任务，尚未真实创建模型任务做端到端验收。不能保证另一个客户端不会手动操作同一线程，因此必须避免跨客户端并发控制。
- Windows 计划任务 CodexMobileMonitor 已实际注册，返回 Ready；普通用户权限，登录触发、失败重启及忽略重复实例。尚未注销/重登或模拟故障验证。
- 主服务实际启动并连接 app-server；认证状态接口返回 connection=connected、managedTasks=[]、schedule=[]。额度端点不可用，quotaStatus=unknown: endpoint unavailable；不是额度为零。
- Webhook、签名密钥、Stream client ID/secret 均未配置，没有真实钉钉外发或手机审批验收。
- 文件审批缺少完整 diff 时仍拒绝批准；桌面原客户端权限弹窗不在本服务可控范围。
- 本轮未接管已有研究线程，未控制桌面/浏览器。

可独立完成的代码、离线验证和登录常驻注册已经推进。余下联网闭环需要用户在本机配置钉钉账号凭据及身份白名单；配置方法见 钉钉本机配置步骤.md，秘密不发送到聊天。

## 2026-11-13：小时守护与Windows通知的现场复核

- 新增完整回复SQLite日志、Windows Toast/托盘回退、跨重启通知去重、限定任务/目录/完整命令匹配的自动审批规则；29项离线测试通过。
- 回复通知正文已经使用实际回复摘要；待审批通知显示操作摘要。Windows通知脚本已实际调用，返回`balloon_api_called`，代表托盘通知接口调用成功，不代表已目视确认显示。
- 使用本机令牌在内存中查询`/api/status`，现有服务返回`connection=connected`，但`managedTasks=[]`、`schedule=[]`、`windowsNotification`未提供。不能将旧服务在线等同于新增功能已部署。
- 独立开发任务`01a1d43b-7a50-7612-8bd4-b6dd122b9c70`仍为`waitingOnApproval`。其待审批工具调用是沙箱外启动并检查计划任务：`Start-ScheduledTask -TaskName CodexMobileMonitor`及对应Get-ScheduledTask/Get-ScheduledTaskInfo核验；没有证据表明这条调用已获批准或执行。
- 每小时唤醒现有医学研究线程、该线程所有权/状态绑定、回复日志到通知的真实端到端链路尚未验收。尚不能宣称守护已全天生效。当前并未为绕过额度或线程所有权创建替代研究任务。

### 同日审批通过后的部署核查

- 用户在根任务批准启动和进程身份核查后，确认四个重复监听8765的进程均属于本项目，已清理并重启已注册计划任务。
- 增加Windows独占端口绑定及重复实例回归测试；30项测试全部通过。
- 实际认证状态接口返回`serviceVersion=0.3.1-windows`、`connection=connected`，端口只有一个监听PID。新版后台服务已实际部署，不能再把旧服务在线当成新版本运行。
- `probe_research_binding.py`仅执行只读检查，未启动模型。当前连接不拥有目标研究线程，`thread/read`和`thread/goal/get`均返回RPCError。因此既有研究任务的自动唤醒尚未接通；不是审批一通过就已经完成端到端链路。
