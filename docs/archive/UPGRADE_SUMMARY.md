# 双客户端更新记录

> 历史更新记录（2026-11-14）。模块与脚本名反映当时布局；当前用法见 [双客户端说明](../MULTI_CLIENT.md)，文件迁移见 [整理记录](REORGANIZATION_2026-11-14.md)。

2026-11-14：在现有桌面和手机网页界面添加 Codex / Claude Code 切换。保留正式入口 `desktop_app.py`、`monitor.py` 及 Codex 原有调度、记录、审批和托盘行为。

此前目录中的多客户端文件是草稿：`claude app-server --stdio` 不是 Claude Code 的协议，`desktop_qt_v2.py` 没有刷新或按钮事件，`monitor_multi.py` 没有完整接入原调度和回复流程。现在 Claude 改用已安装 CLI 的 stream-json 控制通道，两个旧草稿入口转向正式实现。

主要改动：

- `claude_rpc.py`：会话恢复、发送指令、中断、单次工具审批，以及无模型调用的测试任务草稿。
- `claude_sessions.py`：增量读取会话 JSONL，发现任务、对话和最近活动，识别原窗口是否仍打开。
- `monitor.py`：两个独立后台，Claude 使用独立数据库；HTTP 请求按 `client` 路由。
- `desktop_qt.py` / `index.html`：两个切换按钮，分开任务选择、消息草稿、对话、审批和日志。
- `build-exe.ps1`：支持 `-AppName`，构建时保留 release 中已有配置和令牌。
- `test_claude.py` / `test_dashboard.cjs` / `smoke_desktop.py`：覆盖协议、观察与控制边界、消息去重、审批、增量读取和真实 Qt 按钮切换。

使用和限制见 [MULTI_CLIENT.md](../MULTI_CLIENT.md)。未把模拟协议测试描述成真实模型端到端验收；Claude 自动每小时续跑和官方剩余额度不可用，界面明确提示。
