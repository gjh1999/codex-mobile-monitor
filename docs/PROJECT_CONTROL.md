# 跨设备项目控制

选择一台常在线的 Windows 电脑运行协调服务。各开发机用同一项目标识登记本机路径，分别持有设备凭据。人打开已登记的项目或在登记目录运行终端适配器时，后台停止发出该项目的新推进、消息和批准。原 Codex 后台在确认其连接上的任务空闲、审批已结清后退出原生连接。最后一个人工会话关闭后，后台按原有任务设置重新接入。

## 运行中心与登记设备

从澜汐源码目录执行。服务使用已有的 `requirements-web.txt`，将独立的 SQLite 数据库和设备登记文件放在本机私有目录。每台设备配置不同的随机令牌。下面的令牌字段仅为占位内容。

```json
{
  "office": {"token": "<替换为公司机的随机令牌>", "projects": ["vision"]},
  "laptop": {"token": "<替换为笔记本的随机令牌>", "projects": ["vision"]},
  "home": {"token": "<替换为家庭机的随机令牌>", "projects": ["vision"]}
}
```

```powershell
.\.venv\Scripts\python.exe coordination_server.py --devices '.local\devices.json' --database '.local\coordination.sqlite3'
```

中心默认监听 `127.0.0.1:8770`。跨设备时通过可信 HTTPS 入口转发到这个服务。各开发机在自己的 `local-config.json` 中加上以下配置，并把本机令牌设为进程环境变量 `LANXI_DEVICE_TOKEN`。重启桌面或 Web 后台以重新读取项目映射。启用前先退出该项目原有的澜汐后台和 Codex 写入连接。

```json
{
  "coordination": {
    "url": "https://lanxi.example",
    "device": "office",
    "projects": {
      "vision": {
        "path": "D:\\Work\\Vision",
        "threads": ["替换为已跨机交接的 Codex 线程 UUID"]
      }
    }
  }
}
```

两端的目录可以不同，项目标识保持一致。原生记录已使用本机路径的线程按目录识别。对照 [代码与会话交接](HANDOFF.md) 恢复的线程应登记 ID，使后台能够用恢复回执中的目标路径重新接入。后台接入前需要本机已有对应原生记录。

## 人工活动与失败恢复

- 加载 `scripts/lanxi-terminal.ps1` 后，在登记目录执行裸命令 `codex`。适配器先上报人工占用，等后台退出原生连接后再启动 CLI。同一项目同时允许一个登记的 CLI 会话和多个 IDE 窗口。CLI 正常退出时会关闭活动记录。
- IDE 上报端用 `POST /api/v1/projects/{project}/activity` 发送 `session`、`source`、`sequence`、`event`。每个窗口与工作区组合使用唯一会话 ID，`source` 为 `vscode` 或 `pycharm`，序号递增，依次使用 `open`、`heartbeat` 和 `close`。多根工作区对各登记项目分别上报。请求头为 `X-Lanxi-Device` 与 `Authorization: Bearer <设备令牌>`。通过命令行上报可用 `coordination_client.py activity vision vscode <会话ID> <序号> <事件>`。
- 失联 30 秒的窗口显示为 `stale`，仍保留人工占用。确认窗口及其终端实际退出后，从同一设备以更高序号上报 `close`。后台发送结果未知时保留写入记录。协调机可用 `--database <路径> --inspect <项目>` 查询。确认旧后台及相关执行已经退出后，再使用 `--release-confirmed <项目> <版本> <设备> <线程>` 解除对应记录。
- 设备上报在中心事务中撤销后续自动写入许可。已取得发送许可的请求由旧连接继续核对，终端会等待后台退出。若同一后台连接还持有其他活动任务，也会一并等待。挂起的审批可拒绝，或由操作者暂停旧轮次。

本版协调范围为澜汐的 Codex 后台和使用适配器的裸 `codex`。编辑器自动上报扩展、未经过适配器的原生调用、Claude Code 与三台物理机联调仍待接入或验证。

## 桌面与 Web 共用后台

在安装 Web 依赖的源码环境中设置 `MONITOR_EMBED_WEB=1` 及既有的 `MONITOR_PASSWORD`，再启动 `desktop_app.py`。托盘、桌面和 Web 共用一个工作进程及其状态。Web 默认监听 `127.0.0.1:8765`，端口可用 `MONITOR_PORT` 修改。桌面退出时一并停止 Web 服务。前端登录后可调用 `GET /api/v1/project-control` 查询各登记项目的版本、后台持有者和人工占用。

## 复验

```powershell
.\.venv\Scripts\python.exe -m unittest -v tests.test_coordination tests.test_web
.\.venv\Scripts\python.exe tests/smoke_handoff.py --output build-exe/verification/coordination-input
.\.venv\Scripts\python.exe tests/smoke_coordination.py --fixture build-exe/verification/coordination-input --output build-exe/verification/coordination-local
```

使用新的输出目录。真实连接测试启动独立的回环服务和三份虚构设备身份，从第一阶段验收样本复制一条完成的线程。测试确认旧连接释放前原生写入锁仍有效，人工报到后旧后台无法发送新轮次，交还后人工连接可接手，随后后台重新接入。
