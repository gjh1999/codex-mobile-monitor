# 澜汐 Web 工作台

现有 Python 后端 + FastAPI 接口 + React / TypeScript / Vite 前端。Windows 浏览器和手机共用一套界面；Qt 入口保留。本版接入本机任务，尚未实现多电脑云端中继、Tauri 安装包或推送通知。

桌面在同一窗口并排显示任务与对话。手机从任务列表进入对话，通过底部导航进入审批与额度。打开待审批操作可查看后端提供的命令、工作目录和其他细节。手机上可以下拉弹层顶部手柄关闭，键盘和触控也可使用关闭按钮。系统启用「减弱动态效果」时，页面切换直接完成。

## 安装与启动

在仓库根目录执行（Node.js 22+、Python 3.10+；本次验证使用 Python 3.14）：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-web.txt
Push-Location web
npm.cmd ci
npm.cmd run build
Pop-Location
$env:MONITOR_PASSWORD = Read-Host '设置至少16个字符的访问密码'
.\.venv\Scripts\python.exe web_server.py
```

访问 `http://127.0.0.1:8765`。先退出旧 Qt 程序（包括系统托盘），再启动 Web 后端。新代码通过 OS 文件锁防止同一目录重复启动后台；尚未更新的旧 EXE 不认识此锁，仍需手动退出。只启动一个后端进程，不能使用多个 Uvicorn workers 或热重载真实监控服务。

需要同时使用桌面和 Web 时，可在源码启动桌面前设定 `MONITOR_EMBED_WEB=1` 与 `MONITOR_PASSWORD`，共用桌面后台。项目控制接口及配置见 [跨设备项目控制](PROJECT_CONTROL.md)。

登录会话有效期 12 小时，服务重启后重新登录。登录密码只读取环境变量，不写入仓库。Codex / Claude CLI 及其登录、项目配置继续使用原有设置。启动真实后端会恢复现有配置中的任务调度，不是静态界面预览。

## iPhone 真机调试

手机和电脑接入同一个 Wi-Fi。启动前设置：

```powershell
$env:MONITOR_HOST = '0.0.0.0'
.\.venv\Scripts\python.exe web_server.py
```

通过 `ipconfig` 查找电脑的局域网 IPv4，在 Safari 打开 `http://该IP:8765`。如防火墙拦截，仅允许所需端口的专用网络访问。手机页面适配 390px 等窄屏，使用 Safari“添加到主屏幕”可创建入口。本版不提供离线缓存。

需要热更新时，保持后端运行，另开终端：

```powershell
Set-Location web
npm.cmd run dev -- --host 0.0.0.0
```

此时手机访问 `http://电脑IP:5173`，Vite 将 API 请求代理到本机 8765。手机测试须检查软键盘、滚动、切后台恢复；浏览器模拟不等于 Safari 真机验收。

跨公网部署必须配置 HTTPS 反向代理，设置 `MONITOR_ORIGIN=https://你的域名` 和 `MONITOR_HTTPS=1`；不要直接把开发服务暴露到公网。当前接口是单用户私人控制台，尚无多租户权限体系。

## 已迁移功能

- Codex / Claude 切换、任务发现与搜索、历史对话。
- 接入、移出、暂停、继续、重命名、创建测试任务。
- 发送消息、按任务保留草稿；失败/结果不确定时显示错误并恢复未被新草稿覆盖的消息。
- 自动推进开关调用原有 manage/unmanage，不另建调度器。
- 命令、文件、权限审批显示具体详情并调用原有单次审批。
- 显示官方短/长周期用量及重置时间；未知保持未知。
- 密码登录、HttpOnly Cookie、来源检查、写请求校验、登录频率限制。

状态与对话目前每两秒轮询；后台执行结果才代表操作完成。审批结果不确定不显示成功。后续可将状态更新改为 WebSocket；当前仍不支持关闭手机页面后收到系统推送。复杂 Markdown 暂按可换行的纯文本显示，避免把任务内容作为 HTML 执行。

## 验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p 'test_*.py' -q
Push-Location web
npm.cmd run build
npx.cmd playwright test
Pop-Location
```

浏览器测试使用本机 Edge 无头模式、模拟接口，输出 `web/test-results/lanxi-v1/` 截图。测试不执行真实模型任务、审批或创建新会话。历史验证记录见 [Web 验证记录](WEB_VALIDATION.md)。
