# Web 工作台验证记录

## 2026-11-18 澜汐 Web 第一版

- 目的为在现有 FastAPI 工作台上实现澜汐的桌面与手机界面，验证操作行为及生产打包。
- 输入沿用两条虚构任务、对话、命令审批及额度数据。环境为 Windows、Python 3.14.7、Node.js 26.13.1、Vite 6.4.6 和本机 Edge。浏览器视口为 1440×1000 及 390×844。
- 在 `web/` 运行 `npm.cmd run build`。产物位于 `web/dist/`，JS 约 246 kB，gzip 约 77 kB，CSS 约 14 kB。上一版记录为 JS 约 243 kB，gzip 约 76 kB，CSS 约 11 kB。两版的设备性能差异未测量。
- 在仓库根目录运行 `python -m unittest discover -s tests -t . -p 'test_*.py' -q`，项目虚拟环境共运行 118 项，117 项通过，1 项跳过，耗时 11.896 秒。仍出现既有 SQLite 及 TestClient 警告。
- 在 `web/` 运行 `node node_modules/@playwright/test/cli.js test`，2 项通过，最后一次总耗时 7.8 秒。覆盖封面加载、任务导航、消息与草稿、审批弹层拖动与反向、额度以及横向布局。截图位于 `web/test-results/lanxi-v1/`，已检查桌面和手机布局。
- 使用 FastAPI TestClient 禁止启动 worker，验证生产首页、CSS、JS 和壁纸返回 200，未登录访问状态接口返回 401。
- 在本机运行 `codex --version` 和 `codex resume --help`，确认 Codex CLI 0.169.1 支持按会话 ID 恢复并使用 `--cd` 指定目录。仍需用两台 Windows 设备上的独立会话和目录验证跨机恢复。iPhone Safari 真机的手感与软键盘、真实模型操作均待验收。

## 2026-11-17：首版本地 Web 迁移

- 目的：复用已有任务控制后端，以 React 页面替换 Qt 的日常使用界面。
- 环境：Windows 11 专业版，Python 3.14，Node.js 26.13.1，React 19，Vite 6.4.6，FastAPI 0.163.1；精确前端版本见 `web/package-lock.json`。
- 输入：现有离线回归数据、模拟 Codex 任务/对话/审批/额度；未使用用户真实任务或发送模型请求。
- 构建：`cd web; npm.cmd run build` 通过。当前生产 JS 约 243 kB，gzip 约 76 kB，CSS 约 11 kB。
- 接口测试：鉴权、跨源请求、写请求校验、操作转发、未知审批结果、客户端参数校验通过。
- 首次全套回归：118 项，1 错误、1 跳过；Windows 已锁定字节不可读取导致实例锁测试失败，修正为检查文件大小后锁定。修复后实例锁单测通过。
- 修复后完整回归：118 项，0 失败，1 跳过，耗时 11.743 秒。存在既有 SQLite 资源释放警告及 TestClient 依赖弃用提示，不影响断言结果。
- 浏览器：Edge 无头模式，1440×1000、390×844 两种视口；消息发送、按任务草稿、审批、额度和无横向溢出断言通过；截图位于 `web/test-results/desktop-workspace.png`、`web/test-results/iphone-workspace.png`（本机生成、不提交）。已人工查看两张截图。
- 浏览器测试在沙箱内两次断言通过，但服务进程清理卡住，手动中断。改用直接 Node 启动 Vite，并在获准的沙箱外重跑：2 项通过，5.1 秒，退出码 0。未把中断的运行计作完整成功。
- 基线：沿用已有 Qt 后端；没有测量吞吐量、CPU、内存、云服务器容量或与 Qt 的性能差异。
- 未验收：iPhone Safari 真机、真实模型收发和审批往返、云端多电脑中继、打包客户端。
