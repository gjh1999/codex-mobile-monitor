# 澜汐 · Codex / Claude Code 桌面工作台

Windows 原生桌面软件，用于集中查看和控制本机 Codex / Claude Code 任务。澜汐在整窗轮换壁纸上以两块半透明毛玻璃展示任务和对话，提供任务接入、指令发送、暂停与继续、单次审批和自动推进。两端使用独立后台和状态文件。钉钉通知与 Stream 指令是可选通道。

源码仓库和本地目录均使用 `mobile-monitor`。[GitHub 仓库](https://github.com/gjh1999/mobile-monitor)

## 快速开始

R33 私人 Windows 便携版启动时可选「连接云服务器」或「本机使用」。第一次使用再设置项目母目录。你的电脑使用同一私人包连接后会加入同一个工作区。窗口顶部任务栏和主窗口左下角都提供「连接云服务器」。配置、历史和随记可沿用。 [安装、升级与验收](docs/PORTABLE_CHECK.md)

- 从托盘「跨机接续」邀请或配对设备。项目按母目录下的相对路径对齐，代码经 GitHub 同步，项目记忆和已完成的 Codex 会话经协调服务接续。「吉光片羽」的随记、待办、归档及手动分组也会同步。 [个人设备自动接续](docs/PERSONAL_SYNC.md)
- 同一项目可以在设备间交还和接入，也可通过 VS Code 或 PyCharm 插件上报 IDE 占用。达到 100 MiB 的文件可以查看清单，按需借用和回传。 [项目占用](docs/PROJECT_CONTROL.md#个人版项目占用) · [借用与回传](docs/PORTABLE_CHECK.md#跨机借用与回传大文件)
- 「灯下对谈」支持 Markdown 和 Codex 终端历史。澜汐接入的 Codex 会话会流式显示正文，也会在「执行札记」显示命令进度。主窗口左下角和托盘的「测试」提供本机自检和双机回执。 [桌面使用说明](docs/DESKTOP.md) · [运行验收](docs/PORTABLE_CHECK.md#运行验收)

关闭窗口后程序留在系统托盘，从托盘选择「退出程序」即可停止后台。独立 Web 工作台的运行与 iPhone 局域网调试见 [Web 工作台说明](docs/WEB.md)。

在 PowerShell 中从项目根目录启动：

```powershell
& './.venv/Scripts/python.exe' './desktop_app.py'
```

`desktop_app.py` 是原生桌面入口，`monitor.py` 在同一进程管理后台。构建、登录启动与故障检查见 [运行与维护](docs/RUNBOOK.md)。

源码版已通过窗口顶部菜单验收。将鼠标移至窗口内的顶边，点左上角「澜汐」即可使用系统托盘的全部命令。此改动待随便携包发布。 [桌面使用说明](docs/DESKTOP.md#窗口与后台)

## 文档导航

| 要做的事 | 文档 |
|---|---|
| 查看主文档与当前功能入口 | [跨设备开发工作台方案](docs/跨设备开发工作台方案.html#current) |
| 查找全部说明 | [文档索引](docs/README.md) |
| 查看最新交付、候选版和剩余验收 | [跨设备方案交付进度](docs/DELIVERY_STATUS.md) |
| 在云服务器运行设备配对和记忆同步 | [澜汐云协调服务](docs/CLOUD_COORDINATOR.md) |
| 启动、安装依赖、打包、测试、登录启动 | [运行与维护](docs/RUNBOOK.md) |
| 配置键、默认值、环境变量和数据位置 | [配置说明](docs/CONFIGURATION.md) |
| 了解模块职责和后台处理流程 | [架构与文件说明](docs/ARCHITECTURE.md) |
| 预备跨机 Git 工作区、校验 LFS 对象和续接 Codex 线程 | [代码与会话交接](docs/HANDOFF.md) |
| 登记开发机、协调后台写入权及提交硬件计算作业 | [跨设备项目控制](docs/PROJECT_CONTROL.md) |
| 使用桌面窗口、托盘及 EXE | [桌面使用说明](docs/DESKTOP.md) |
| 切换客户端、理解控制与自动推进边界 | [双客户端使用说明](docs/MULTI_CLIENT.md) |
| 配置 Webhook 通知与 Stream 指令 | [钉钉配置](docs/DINGTALK_SETUP.md) |
| 查询过去的需求、计划和验收记录 | [历史文档](docs/archive/README.md) |

## 文件布局

```text
mobile-monitor/
├── README.md                   # 项目入口
├── desktop_app.py              # 桌面启动入口
├── desktop_qt.py               # 完整 Qt 双客户端工作台与托盘
├── desktop_glass.py            # 壁纸轮换、毛玻璃层和客户端滑块
├── desktop_theme.py            # 蓝白主题、字体和消息排版
├── desktop_win32.py            # Win32 回退界面
├── monitor.py                  # 进程内双客户端后台与状态编排
├── *.py                        # RPC、任务控制、调度、审批、通知和消息记录
├── local-config.example.json   # 无凭据的配置示例
├── requirements-*.txt          # 桌面、构建和可选 Stream 依赖
├── docs/                       # 当前使用与维护文档
│   └── archive/                # 历史需求、计划与验收记录
├── scripts/                    # 构建、登录启动、通知检查
├── tests/                      # Python 回归与 Qt smoke
├── schema/                     # Codex app-server 协议参考
├── archive/                    # 原型、诊断、人工维护工具及旧构建快照
├── build-exe/                  # 本机构建输出（忽略）
└── release/                    # EXE 及其独立运行数据（忽略）
```

源码运行的配置、SQLite 数据库和日志位于项目根目录。R21 起，便携 EXE 使用 `%LOCALAPPDATA%\Lanxi\data`，换安装目录也会读回原数据。`.venv/` 和这些本机文件均由 `.gitignore` 排除。 [配置说明](docs/CONFIGURATION.md) · [归档索引](archive/README.md)

## 功能边界

- 发现任务、只读关注、取得控制权和启用自动调度是不同状态。Codex 自动续跑需要本连接拥有控制权、实时空闲、有效目标且无待审批。额度耗尽时保存刷新时间并等待，到点重新查询；服务端确认额度恢复后继续原任务。手动暂停、关闭自动推进或移出会取消自动恢复。
- Claude Code 支持历史查看、手动消息、暂停、继续和单次工具审批。每小时自动推进默认关闭，显式开启后仅在可控制、空闲且无审批或执行错误时发送；任务完成后需关闭。官方剩余额度显示未知。
- 自动审批只匹配显式配置的 Codex 任务、目录与完整命令；其他请求由人工核对并单次处理。
- 钉钉 Webhook 用于出站通知，Stream 指令另需应用凭据和身份白名单。
- 离线测试与界面检查不代替真实模型收发、工具审批往返、系统通知展示、钉钉联网或 Windows 重新登录验收。历史结果仅适用于记录当时的范围。

## 验证

```powershell
& './.venv/Scripts/python.exe' -m unittest discover -s tests -t . -p 'test_*.py' -v
& './.venv/Scripts/python.exe' './tests/smoke_desktop.py' --output "$env:TEMP/monitor-dual-client-smoke"
```

Qt smoke 使用虚构任务和替代后台，点击生产界面并生成截图。验证范围及操作说明见 [运行与维护](docs/RUNBOOK.md)。
