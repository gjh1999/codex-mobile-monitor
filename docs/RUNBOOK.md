# 运行与维护

[文档索引](README.md) · [配置说明](CONFIGURATION.md)

以下命令使用 PowerShell，均从项目根目录执行：

```powershell
Set-Location 'C:/Users/gjh/PycharmProjects/To_Do_List/codex-mobile-monitor'
```

## 环境准备

后台通过本机 CLI 工作：Codex 使用 `codex app-server --stdio`，Claude Code 使用 CLI 的 `stream-json` 通道。需要使用哪一端，就先安装并登录对应 CLI；登录和模型配置由 CLI 自己管理。

已有 `.venv` 时可直接使用。首次创建环境：

```powershell
py -m venv .venv
& './.venv/Scripts/python.exe' -m pip install -r requirements-desktop.txt
```

| 文件 | 用途 |
|---|---|
| `requirements-desktop.txt` | 完整 Qt 桌面界面及 Qt smoke |
| `requirements-build.txt` | Qt 和 PyInstaller，构建 Windows EXE |
| `requirements-stream.txt` | 可选的钉钉 Stream SDK |
| `requirements-lock.txt` | 既有 Stream 环境的锁定依赖快照，不含 Qt / PyInstaller |

没有配置 Stream 就不会导入该 SDK。首次配置可从 [示例文件](../local-config.example.json) 复制；以下命令只在文件不存在时创建，保留已有私密配置：

```powershell
if (-not (Test-Path './local-config.json')) {
    Copy-Item './local-config.example.json' './local-config.json'
}
```

示例中的受管任务默认关闭。配置含义和路径规则见 [配置说明](CONFIGURATION.md)。

## 启动与停止

源码启动的正式入口：

```powershell
& './.venv/Scripts/python.exe' './desktop_app.py'
```

双击 `release/Lanxi.exe` 打开原生桌面工作台。关闭窗口会缩到托盘，从托盘选择“退出程序”停止后台。源码环境缺少 Qt 时可以使用功能较少的 Tk 或 Win32 回退界面。

桌面进程通过内部模块 `monitor.py` 启动两个客户端的后台工作线程。该模块不提供独立启动服务。`desktop_qt_v2.py` 是保留的兼容转发，日常使用 `desktop_app.py`。

源码运行和 EXE 运行的数据位置不同，见 [桌面说明](DESKTOP.md)。避免同时运行不同版本并控制相同任务；更新前先从托盘退出旧实例。

## 构建 EXE

```powershell
& './.venv/Scripts/python.exe' -m pip install -r requirements-build.txt
& './scripts/build-exe.ps1'
```

默认输出为 `release/Lanxi.exe`。临时构建数据及 `.spec` 位于 `build-exe/`。可用其他文件名构建预览包。

```powershell
& './scripts/build-exe.ps1' -AppName Lanxi-preview
```

0.7.3 的后台版本标识为 `0.7.3-desktop-only`，修复先隐藏窗口到托盘、再选择退出时 Qt 主循环残留的问题。退出时显式结束应用循环，清理后台与托盘，并在主循环结束后释放单实例锁。保留 0.7.2 的 Codex 继续按钮修复，以及 0.7.1 的暂停误报与额度恢复功能。新版使用独立文件名交付，旧 EXE 未被覆盖。

构建脚本只在 `release/` 尚无 `local-config.json` 时复制根目录配置；不会同步或覆盖已有的发布配置，也不会复制数据库。分享 EXE 时不要连同私密配置、数据库和会话草稿一起分享。替换正在使用的 EXE 前，先从托盘退出程序。

EXE 已包含 Python 界面依赖，仍需要对应本机 CLI。其运行状态写在 EXE 同目录，保留该目录才能保留任务记录。

## Windows 登录启动

以下命令会注册或移除当前用户的计划任务，按需执行：

```powershell
& './scripts/install-startup.ps1'
Get-ScheduledTask -TaskName CodexMobileMonitor | Select-Object TaskName, State, Actions
& './scripts/install-startup.ps1' -Remove
```

脚本优先选 `release/Lanxi.exe`，其次查找旧版 CodexMobileMonitor 桌面程序，最后回退到 `.venv/Scripts/pythonw.exe desktop_app.py`。使用普通用户权限，登录触发，配置失败重启和忽略重复实例。已有计划任务需重新运行脚本才会采用新入口。

从旧版本迁移后，检查已有计划任务的实际 `Actions`；入口不是当前桌面程序时，重新执行安装脚本更新它。注册成功不等于重新登录和故障重启已经实测。设置环境变量后应重启对应进程，使其继承新值。

## 验证

Python 回归测试：

```powershell
& './.venv/Scripts/python.exe' -m unittest discover -s tests -t . -p 'test_*.py' -v
```

`-t .` 将项目根目录加入测试导入路径。测试使用临时存储和假 RPC，不启动真实模型或向钉钉外发。Stream SDK 未安装时，对应离线 SDK 测试会跳过。

Qt smoke 使用虚构会话、替代后台和临时数据，点击双客户端按钮并输出截图：

```powershell
& './.venv/Scripts/python.exe' './tests/smoke_desktop.py' --output "$env:TEMP/monitor-dual-client-smoke"
```

它验证生产 Qt 界面的事件接线和显示状态。实际验收还应检查真实 CLI 收发、模型回复、单次工具审批往返、Windows 通知展示，以及重新登录后的启动行为；钉钉联网按其配置文档单独核验。上述命令是检查方法，不表示每次构建都已完成这些现场验收。

## 常见检查

| 现象 | 检查 |
|---|---|
| `python` 找不到或指向 Microsoft Store | 使用项目 `.venv/Scripts/python.exe` 的明确路径 |
| 没有完整双客户端界面 | 安装 `requirements-desktop.txt`，从 `desktop_app.py` 启动 |
| 托盘退出后没有窗口但仍有旧监控进程 | 0.7.2 及更早版本可能遗留 Qt 主循环；升级到 0.7.3。新程序不会自行清理已残留的旧版本，应确认完整路径和进程树后单独处理，勿按 `codex.exe` 名称批量结束 |
| 再次打开没有新窗口 | 查看系统托盘；桌面单实例机制会阻止重复启动 |
| 登录后没有窗口 | 检查计划任务实际入口、状态和本机 CLI；确认程序是否在托盘中 |
| 修改配置后没有效果 | 确认改的是源码根目录还是 EXE 目录；自动接入和 Stream 身份在启动时读取，修改后重启 |
| 重新接入后 Codex 已运行但不能发消息、“继续”灰色 | 先从托盘退出旧版，再打开 0.7.2；有控制权时点击“继续任务”，只解除本地暂停，不重复启动任务 |
| Codex 没有自动续跑 | 检查自动推进开关、控制权、实时空闲、active goal 和待审批 |
| Codex 额度用完后等待 | 查看“等待额度刷新”的时间；到点会重新查询，服务端确认可用后继续原任务。手动暂停、关闭自动推进和移出会取消恢复 |
| 额度恢复但旧版仍拒绝继续 | 先从托盘退出旧版，再打开 0.7.1；手动继续允许重新尝试，预算受限或已完成目标仍需核对 |
| Claude 没有自动推进 | 默认关闭；检查是否手动开启、已到一小时检查时间，且可控制、空闲、无审批或执行错误 |
| 额度显示未知 | 当前没有有效官方数据，不代表额度为零；Claude 本实现没有官方额度查询能力 |
| 钉钉没有响应 | 分别检查 Webhook / Stream 凭据、Stream SDK、私聊身份三元组，见 [钉钉配置](DINGTALK_SETUP.md) |

备份前退出相应实例，复制其完整状态目录，保留 SQLite 文件及存在的 sidecar 文件。恢复和重新构建时保留私密配置；详细文件清单见 [配置说明](CONFIGURATION.md)。
