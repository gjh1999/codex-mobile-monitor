# 目录整理记录（2026-11-14）

目标：将 `codex-mobile-monitor` 的当前用法、测试、运维脚本和历史资料分开，纠正文档中过时或互相冲突的操作说明，同时保留应用运行所需的原路径和本机数据。

## 文件处理

- 为源码入口建立 `docs/README.md`、`RUNBOOK.md`、`CONFIGURATION.md`、`ARCHITECTURE.md`，并更新双客户端、桌面和钉钉当前指南。
- 将既有 `test_*.py`、网页测试和 Qt smoke 移到 `tests/`；网页测试改为从上一级读取正式 `index.html`，桌面 smoke 将项目根加入导入路径，unittest 通过 `-t .` 发现测试。
- 将 EXE 构建、登录启动、Webhook 检查放入 `scripts/`；图标生成器放入 `scripts/dev/`。PowerShell 脚本由 `PSScriptRoot` 推导项目根目录；Python 通知脚本自行加入根目录导入路径。
- 旧网页、旧布局生成器、多客户端原型、只读诊断、goal 人工工具及机器专用旧 `.spec` 放在 `archive/`。历史需求计划和阶段验收记录移到 `docs/archive/`。
- 当前 HTTP 页面仍保存在 `index.html`，运行时数据仍位于源码项目根目录或 EXE 自身目录；未移动令牌、私密配置、数据库、日志、`.venv/`、`build-exe/`、`release/`。
- 增加 `requirements-desktop.txt` 与 `requirements-build.txt`，将 Qt / PyInstaller 环境依赖显式记录。已有 Stream 依赖清单和锁文件保留。
- `.gitignore` 覆盖本机凭据、草稿、SQLite 及 sidecar、日志、Python 缓存、虚拟环境和构建/发布目录。

## 明确保留的兼容路径

`monitor.py`、`desktop_app.py`、`desktop_qt.py`、`desktop_win32.py`、`windows_notify.ps1`、`app.ico`、`index.html`、配置示例、requirements 文件和 `schema/` 保持原位。这样源码入口、运行目录计算、网页服务、PyInstaller 数据收集及已注册任务的 EXE 路径不因目录归类改变。

运行时配置明确区分源码根目录与 EXE 目录。计划任务脚本已调整为从 `scripts/` 位置找到项目根目录；已有计划任务使用的应用入口路径不变。

## 验证

整理前：现有 56 项 Python 回归测试通过；网页测试通过。

目录迁移后：56 项 Python 回归测试全部通过，网页测试通过，Qt smoke 在 offscreen 模式下验证了双客户端切换、独立草稿和对话展示。PowerShell 构建及登录启动脚本的语法、根目录和资源路径检查通过；37 个维护中的 Python 文件语法检查通过。新增桌面和构建依赖清单经 pip dry-run 确认与本机安装匹配，未修改环境。

本轮没有重建或替换正在使用的 EXE，也没有重新注册计划任务；构建和登录启动验证限静态检查。最终已通过 Markdown 本地链接检查、Python 语法检查、依赖 dry-run 和本机运行数据忽略规则检查。对照整理前备份，389 个原有维护文件全部保留，其中 371 个内容未变、18 个更新了文档或辅助路径。

回归过程中发现 Claude 假进程读取线程尚未退出就关闭临时 SQLite 的测试清理竞态；测试现先等待读取线程退出再关闭数据库。调整后独立 12 项 Claude 测试和全量 56 项测试均通过，未再出现后台线程清理异常，应用运行代码未因此改动。

这些检查验证路径和界面，不代表真实模型调用、DingTalk 网络、iPhone 收发或 Windows 重新登录验收。
