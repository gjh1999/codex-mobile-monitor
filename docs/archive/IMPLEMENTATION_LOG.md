# 实施与验收记录

> 历史验收日志。各条结论只描述记录当时的版本和环境；旧文件名与路径的迁移见 [整理记录](REORGANIZATION_2026-11-14.md)，当前操作见 [运行与维护](../RUNBOOK.md)。

## 2026-11-18 Git LFS 按需预取与续传

- 基线 `cf0f3d9` 的交接流程只支持普通 Git 文件。本轮增加指定提交及路径的 LFS 缓存预取和离线检查。环境为 Windows、Python 3.14.7、Git 2.56.0.windows.4 和 Git LFS 3.8.1。测试服务器仅监听回环地址，数据为确定性生成的测试对象。两个路径指向同一个对象，验证批量请求和对象去重。
- `.\.venv\Scripts\python.exe -m unittest -v tests.test_lfs_assets` 首轮 5 项通过，3.545 秒。补验完全下载但未发布的临时对象后，6 项通过，3.445 秒。覆盖缺件、错误 SHA-256、缓存损坏、错误 Range、服务器忽略 Range、拒绝跨站或跳转、并发去重与纯本机检查。日志为 `build-exe/verification/lfs-targeted-01.txt` 和 `lfs-targeted-02.txt`。
- `.\.venv\Scripts\python.exe tests/bench_lfs_assets.py --output build-exe/verification/lfs-64m-01` 首轮下载与续传通过。原生检出断言失败，夹具缺少本地 Git LFS 过滤器且辅助 Git 命令设置了跳过 smudge。该轮未生成 `result.json`。夹具在本地注册过滤器，随后以标准 Git LFS 命令复验。
- 上述基准命令换用 `build-exe/verification/lfs-64m-02`。64 MiB 全量预取 0.522 秒。在 16 MiB 强制断开时用时 0.329 秒，缓存未就绪。下一次请求为 `Range: bytes=16777216-`，实际传输 48 MiB，用时 0.486 秒。该次重试比从头重试少传 16 MiB。随后离线检查 0.493 秒，关闭服务后由原生 Git LFS 检出并校验文件用时 0.648 秒。都是单次本机墙钟时间，包含命令进程启动。结果见输出目录的 `result.json`。未测远程吞吐和峰值内存。
- 在包含并行桌面提交 `9253b24` 的源码上，运行 `.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p 'test_*.py' -v` 共 184 项，183 项通过，1 项可选 Stream SDK 跳过，153.310 秒。日志为 `build-exe/verification/lfs-regression-01.txt`。
- 家庭 Gitea、真实 VPN 传输、iPhone 真机和 LFS 缓存与工作区交接的就绪联动待验或开发。 [Git LFS 配置及复验](../HANDOFF.md#按需预取-git-lfs-对象) · [Gitea LFS 服务](https://docs.gitea.com/administration/git-lfs-setup/)

## 2026-11-18 空闲工作区自动更新

- 基线 `a096c6d` 需要手动选择合并目录和原路径切换。本轮把已签名的接收、按需合并和原路径切换接入同一个同步扫描。策略默认关闭，启用后绑定设备、线程和本机分支。环境为 Windows、Python 3.14.7、Git 2.56.0.windows.4 与 Codex CLI 0.169.1。仓库、设备和回复均为本机测试数据。
- `.\.venv\Scripts\python.exe -m unittest -v tests.test_handoff_auto tests.test_handoff` 首轮 18 项通过，55.631 秒。覆盖重复扫描和状态文件缺失后继续去重、IDE 占用、本机修改、分支不匹配、预备文件被修改、本机会话领先、切换前收到新版、签名错误和等待恢复。核对同一版本后续扫描的自动应用路径没有 Git 合并或协调查询，也核对完成新本机轮次前阻止自动回传。输出为 `build-exe/verification/handoff-auto-targeted-01.txt`。
- 执行 `.\.venv\Scripts\python.exe tests/smoke_handoff.py --sync --roundtrip --merge --diverged --apply --automatic --stock-files 768 --output build-exe/verification/handoff-auto-native-48m-01`。共享基线为 768 个 64 KiB 文件，共 48 MiB。已预备后触发的首轮自动扫描用时 4.963 秒，包含签名校验、分叉合并、原路径切换及进程启动，更新 4 个工作区文件。下一轮扫描为 0.217 秒，复用原路径回执。上一版的手动切换为 1.606 秒和 1.996 秒，另有独立合并步骤，阶段口径不同。结果见输出目录 `result.json`，C、D 盘符模拟往返，协调及回复服务使用回环地址。未测峰值内存及跨机传输吞吐。
- `.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p 'test_*.py' -v` 全量运行 178 项，177 项通过，1 项可选 Stream SDK 跳过，151.239 秒。日志为 `build-exe/verification/handoff-auto-regression-01.txt`。使用相同参数在 `handoff-auto-native-48m-02` 复验，首次自动应用为 5.292 秒，重复扫描为 0.265 秒，真实 CLI 续写同一线程并再次封存。
- 在 Qt offscreen 下运行 `.\.venv\Scripts\python.exe tests/bench_task_switch.py --output build-exe/verification/task-switch-after-auto`，35 条长消息、首次读取模拟等待 140 毫秒。六次切换的事件循环等待中位数为 1.62 毫秒，历史显示中位数为 13.47 毫秒。上一版同口径为 1.65 毫秒和 9.14 毫秒。结果为输出目录 `result.json`，测试与实际合并分别运行。
- 工作区已存在本地修改时继续保留预备快照。同版本持续应用失败或预备目录被修改后，60 秒内复用上次结果，保留可重试的现场。中断的事务通过 `--recover` 处理。正式启用前需要真实 Syncthing、两台物理设备及两种 IDE 联调，并统计实际额外操作数。 [空闲时自动应用](../HANDOFF.md#空闲时自动应用到原路径)

## 2026-11-18 Windows 原路径切换与中断恢复

- 基线 `1bd5157` 支持在独立目录打开合并结果。本轮为已登记的独立 Git 仓库增加原路径应用和恢复。试验使用 Windows、Python 3.14.7、Git 2.56.0.windows.4 与 Codex CLI 0.169.1，项目、配置、设备身份和模型回复均为测试环境生成。
- `.\.venv\Scripts\python.exe -m unittest -v tests.test_handoff_apply tests.test_coordination` 首轮 12 项，4 项错误，25.135 秒。临时 Codex 根目录未初始化，Windows 原生锁目录创建失败。增加首次运行目录初始化后，第二轮 13 项，2 项错误，26.421 秒。Git 拒绝把 `symref-verify HEAD` 和更新其分支放在同一 Files 引用事务。进一步尝试 `no-deref` 后，2 项复验仍因重复锁定 HEAD 失败，用时 9.421 秒。最终用索引锁防止普通检出，并以预期旧对象 ID 校验后更新指定分支。专项第 3 轮 13 项通过，24.424 秒。三轮存档为 `build-exe/verification/handoff-apply-targeted-01.txt` 至 `handoff-apply-targeted-03.txt`。
- 执行 `.\.venv\Scripts\python.exe tests/smoke_handoff.py --sync --roundtrip --merge --diverged --apply --output build-exe/verification/handoff-apply-native-01`，再用 `--stock-files 768 --output build-exe/verification/handoff-apply-native-48m-01` 复验。测试用真实 CLI 和回环协调服务，在 C、D 盘符内模拟往返。在小仓库与 768 个 64 KiB 共享文件的 48 MiB 仓库中，原路径切换命令分别为 1.172 秒和 1.606 秒，每次更新 4 个工作区文件。时长包含进程启动、Git 对象导入与协调请求，独立合并、封存和模型往返另行执行。旧基线没有同路径切换可比数据。未测峰值内存、并行任务切换或实际网络。两轮的 `result.json` 和再次封存的 `merged-onward.zip` 保存在各自输出目录，临时源目录在退出时清理。
- `.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p 'test_*.py' -v` 首轮 171 项，170 项通过，1 项可选 Stream SDK 跳过，111.298 秒。已加入已完成切换但协调服务回复丢失后的恢复测试。输出为 `build-exe/verification/handoff-apply-regression-01.txt`。
- 快进到并行的桌面提交 `ff3f703` 后，同命令第一轮集成回归 171 项，170 项通过，1 项跳过，107.024 秒。把索引复核移到取锁之后，并核对刚移入备份的文件后，集成复验 172 项，171 项通过，1 项跳过，113.769 秒。最后用 `-m unittest -v tests.test_handoff tests.test_handoff_apply.ApplyTests.test_new_worktree_with_old_local_history_cannot_be_published` 补验旧历史配新工作区的发布拦截，14 项通过，33.645 秒。日志分别为 `handoff-apply-integrated-01.txt`、`handoff-apply-integrated-02.txt` 和 `handoff-apply-history-final.txt`。同一 48 MiB 参数在 `handoff-apply-native-48m-02` 再验，原路径切换用时 1.996 秒，成功续接并再次封存。
- 使用 Windows 实际文件句柄模拟占用，验证已移动文件回滚。模拟在新文件、提交与索引已安装后中断，恢复到原索引字节和原目录状态。也验证已登记 IDE、协调后台、原生线程写入、未跟踪路径碰撞及本机已跟踪修改等切换拦截条件。自动更新策略、断电恢复和真实双机 Syncthing 与 IDE 联调仍待完成。[原路径切换与恢复](../HANDOFF.md#应用到本机原路径) · [Git 校验与更新引用](https://git-scm.com/docs/git-update-ref)

## 2026-11-18 不同提交工作区合并与往返续接

- 基线 `f39661a` 支持同 HEAD 的独立预备合并。本轮验证本机领先、接收端领先和历史分叉。存在祖先关系时使用较新的提交。分叉合并成功时，只在预备目录生成双父提交，源端暂存和未暂存修改分别叠加。环境为 Windows、Python 3.14.7、Git 2.56.0.windows.4、Codex CLI 0.169.1。输入为临时 Git 仓库和虚构会话。
- `.\.venv\Scripts\python.exe -m unittest -v tests.test_handoff_merge` 首轮 12 项通过，30.926 秒。覆盖双向快进、双父提交、已提交内容与暂存层的冲突报告、无共同祖先、浅历史、私有路径拦截、大小写冲突、源索引字节和续接回执。输出为 `build-exe/verification/handoff-diverged-targeted-01.txt`。对象格式不兼容随后并入全量回归。
- `.\.venv\Scripts\python.exe tests/bench_handoff_merge.py --output build-exe/verification/handoff-same-48m-03` 和同脚本的 `--diverged --output build-exe/verification/handoff-diverged-48m-01` 顺序运行。每轮为 768 个 64 KiB 的共享基线文件，共 48 MiB，外加少数文本修改。分叉样本另在两端各提交一个不同文件。同 HEAD 成功合并为 3.142 秒，未暂存层冲突为 1.267 秒。上一轮同 HEAD 为 3.334 秒和 1.288 秒。分叉样本为 2.870 秒和 1.334 秒。结果分别保存于两个目录的 `result.json`。成功各调用一次检出和对象打包，冲突均无检出或打包。打包限制两个线程。封存和还原分别为 14.141 秒和 13.979 秒，单独计时。以上均为单次本机墙钟结果，未测峰值内存和网络吞吐。
- `.\.venv\Scripts\python.exe tests/smoke_handoff.py --sync --roundtrip --merge --diverged --output build-exe/verification/handoff-diverged-native-01` 通过。真实 CLI 在 C、D 盘间模拟往返，核对了分叉合并的两个父提交、双方文件内容、暂存层、原生历史锁及同线程续写。新一轮与合并提交再次封存到 `merged-onward.zip`。具体线程和轮次见 `result.json`。传输用本机目录复制，回复用回环服务，单轮耗时未独立记录。
- `.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p 'test_*.py' -v` 共 164 项，163 项通过，1 项可选 Stream SDK 跳过，用时 77.332 秒，日志为 `build-exe/verification/handoff-diverged-regression.txt`。
- 使用 Qt offscreen 运行 `.\.venv\Scripts\python.exe tests/bench_task_switch.py --output build-exe/verification/task-switch-after-diverged`。输入为 35 条长消息、每次首次读取人为延迟 140 毫秒。6 次切换的事件循环等待中位数为 1.65 毫秒，历史显示中位数为 9.14 毫秒。两次冷读显示约 180 毫秒，四次缓存命中为 7.54 至 10.14 毫秒。上次同口径的两个中位数为 1.51 和 8.39 毫秒。实验未同时运行合并，结果见 `result.json`。
- 原路径安全切换、两台物理设备、Syncthing 和两种 IDE 的联调仍待完成。实际使用的额外操作数待测。 [工作区合并与交接说明](../HANDOFF.md)

## 2026-11-18 同提交工作区合并与性能验证

- 基线 `26c0d7e` 可恢复两端各自的目录。本轮加入按需工作区合并，保持两端的 Git 暂存和工作层，以版本 1 的 JSON 报告冲突。运行环境为 Windows、Python 3.14.7、Git 2.56.0.windows.4 和 Codex CLI 0.169.1。测试输入由临时 Git 仓库和虚构会话生成。
- 初次专项运行 4 项，7.520 秒，出现 1 项误报冲突。Windows 的两份工作区采用 LF 和 CRLF，初版按原始字节生成 Git 文本对象。调整后按输入仓库的 Git 换行规则读取跟踪文件，在隔离配置中进行 Git 操作。专项 4 项复测全部通过，8.519 秒。再补合并后才生效的内容过滤器和中途保存检查，6 项通过，15.103 秒。额外核对自定义合并驱动隔离、文本和二进制冲突、原索引字节及显式纳入的新文件。
- 执行 `.\.venv\Scripts\python.exe tests/bench_handoff_merge.py --output build-exe/verification/handoff-merge-48m-02`。数据为 768 个 64 KiB 基线文件，共 48 MiB，加少量暂存和未暂存文本变更。`handoff-merge-48m-01` 的合并成功和冲突用时分别为 3.659、2.054 秒，末次为 3.334、1.288 秒。末次成功只调用 1 次 `merge-tree`、1 次检出和 1 次对象打包。冲突路径执行 2 次 `merge-tree`，检出和打包均为 0 次。同批输入的既有封存与恢复为 13.994 秒，属于独立步骤。结果保存在各轮的 `result.json`，时长为单次本机墙钟实测，未测峰值内存及网络吞吐。
- 执行 `.\.venv\Scripts\python.exe tests/smoke_handoff.py --sync --roundtrip --merge --output build-exe/verification/handoff-merge-native-01`，真实 Codex CLI 往返后在合并目录续写同一线程。已核对 A 独有的暂存文件、B 文件的暂存版和工作版，以及原生写入锁和旧历史备份。文件传输采用本机目录复制，模型回复来自回环测试服务。此前无合并参数的往返复测也通过，用时 8.28 秒。
- 首轮全量回归运行 156 项，56.442 秒，1 项跳过。快进到并行桌面更新 `16f4206` 后的集成复测，156 项中有 1 项 Windows 临时库清理失败，耗时 58.554 秒，日志在 `handoff-merge-integrated-regression.txt`。原因是对话读线程偶尔晚于窗口关闭。改为退出时收尾当前读任务后，运行 `python -m unittest discover -s tests -t . -p 'test_*.py' -v` 共 157 项，156 项通过，1 项可选 Stream SDK 跳过，53.059 秒，结果在 `build-exe/verification/handoff-merge-final-regression.txt`。
- 用与前次相同的 35 条长消息和 140 毫秒读取等待，运行 `python tests/bench_task_switch.py --output build-exe/verification/task-switch-after-merge`。界面事件循环等待中位数为 1.51 毫秒，显示历史中位数为 8.39 毫秒。上一版同口径记录分别为 1.58 毫秒和 8.34 毫秒。本轮 `tests/smoke_desktop.py --output build-exe/verification/handoff-merge-desktop-smoke` 通过，使用 Qt offscreen 模式。
- 当前覆盖同 HEAD 的独立合并结果。不同提交的分支合并、原目录内应用和两台设备的网络验收仍待完成。 [工作区合并与交接说明](../HANDOFF.md) · [Git 三方树合并](https://git-scm.com/docs/git-merge-tree)

## 2026-11-18 跨盘往返续接与原生写入锁

- 基线 `b3b8dbf` 已支持自动发布和接收目录，但返回源端时，早期同线程历史会被判为冲突。本轮验证完整字节前缀的原生 JSONL 快进与重复发布。环境为 Windows、Python 3.14.7、Git 2.56.0.windows.4 和 Codex CLI 0.169.1。输入为虚构的独立仓库、互不共享状态的两个 Codex 目录和本地固定回复服务。
- 用 `.\.venv\Scripts\python.exe tests/smoke_handoff.py --sync --roundtrip --output build-exe/verification/handoff-roundtrip-01` 执行首轮，7.54 秒通过，第二轮 `handoff-roundtrip-02` 为 7.42 秒。第一次快进被真实 app-server 的活动写入锁拒绝。进程退出后，旧历史被独立备份，再原子替换并续写同一线程。模拟发布失败和本机日志独立变动的单测核对了备份字节，也覆盖压缩同名记录、未验证 CLI 版本和未登记的预备目录。
- 用相同命令更换输出为 `build-exe/verification/handoff-roundtrip-paired-01` 验证最终配置。真实 CLI 按 C→D→C 完成三个轮次，最终回复由回环服务在看到 B 的回复后返回。B 的暂存和未暂存两版分别回到 A 的新预备目录，A 的原工作文件保留。B 第二次发布复用第一次接收时的配置，从已登记仓库解析 `origin/main`，自动跟随本次已恢复目录。结果在 `result.json`，原历史及续写后历史各保留一份测试副本。本轮与回归并行，未另计端到端墙钟时间。
- 专项 `python -m unittest -v tests.test_handoff tests.test_coordination` 首轮运行 17 项全部通过，22.461 秒。运行 `python -m unittest discover -s tests -t . -p 'test_*.py' -v` 的两次全量回归分别为 149 项、39.534 秒，以及最后的 150 项、45.620 秒。末次 149 项通过、1 项可选 Stream SDK 跳过，输出为 `build-exe/verification/handoff-roundtrip-paired-regression.txt`。
- 当前快进支持 Windows 上已验证的未压缩原生格式。物理双机、实际 Syncthing、交互 IDE 以及现用工作树合并仍待验，网络时延和用户额外操作数未测量。 [代码与会话交接](../HANDOFF.md) · [Codex 0.169.1 写入锁源码](https://github.com/openai/codex/blob/rust-v0.169.1/codex-rs/rollout/src/writer_lock.rs)

## 2026-11-18 桌面任务切换延迟

- 复现于 Windows、Python 3.14.7、PySide6 6.12.0，基线为 `1a3617d`。用三份虚构任务和每份 35 条长消息，给读取增加 140 毫秒固定等待。执行 `tests/bench_task_switch.py --output build-exe/verification/task-switch-final`，使用 `QT_QPA_PLATFORM=offscreen`。基线用相同脚本和独立归档源码，输出于 `task-switch-baseline-final`。
- 初次基线记录为 337.41 毫秒事件循环等待中位数。初版异步测试连续使用 `QTest.qWait`，Python 工作线程无法及时获得执行时间，三次达到 8 秒超时。改用真实 Qt 事件循环轮询完成状态，异步初次复测的等待中位数为 1.52 毫秒。随后桌面 smoke 的同步断言未等到异步历史读取，改为按条件等待，并覆盖迟到历史不会覆盖当前任务。
- 最终两组串行测量中，六次任务选中后的事件循环等待中位数从 338.19 毫秒降为 1.58 毫秒，显示对话的中位数从 344.38 毫秒降为 8.34 毫秒。新版本首次选中未缓存任务约 178 毫秒显示历史，返回已缓存任务约 7–9 毫秒。测量为本机合成负载，不含远端接入或真实用户数据库。改动将记录读取和富文本排版放入后台，保留最近五个视图的渲染文档与滚动位置。
- `tests/smoke_desktop.py --output build-exe/verification/task-switch-desktop-smoke-02` 通过。全套 `.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p 'test_*.py' -v` 运行 147 项，146 项通过、1 项可选 Stream SDK 跳过，用时 37.993 秒，见 `build-exe/verification/task-switch-regression.txt`。
- 运行 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/build-exe.ps1 -AppName Lanxi-Glass-R8`，PyInstaller 完成构建约 40.89 秒。包位于 `release/Lanxi-Glass-R8.exe`，61,417,833 字节，同目录 R7 为 54,104,555 字节。归档核对包含新版桌面模块、交接后台和 Qt Windows 插件。切换测试执行的是源码窗口，新 EXE 的实机启动和操作耗时未测。

## 2026-11-18 对话气泡圆角与底色

- 以 R11 的对话区为基线，保留正文排版、左右对齐和内边距。普通消息使用 11 像素圆角和更透的浅蓝底。助理及用户气泡的背景 alpha 分别为 132 和 172，色值为 `#EBF9FF` 和 `#DAF0FD`。第一版实机图中，助理气泡背景比原位置向右偏 10 像素。最终根据文本单元格的布局和内边距定位。
- Windows 11 x64、Python 3.14.7、PySide6 6.12.0。用虚构的双客户端短消息、多段长文本、代码和审批请求，在 `build-exe/verification/lanxi-soft-bubbles/r11-release` 设置 `QT_QPA_PLATFORM=windows`，执行 `python tests/smoke_desktop.py --output ../windows-release`。验证两种气泡的圆角处透出毛玻璃，圆角内绘制背景，1020×640 长消息换行和滚动正常。最终图位于 `windows-release`，首轮图留在 `windows-first`。
- 在 `cf0f3d9` 的集成工作树设置 `QT_QPA_PLATFORM=offscreen`，执行 `python -m unittest discover -s tests -t . -p 'test_*.py' -q`，178 项中 177 项通过、1 项跳过，用时 193.026 秒，结果在 `regression.txt`。桌面、会话和项目分组专项共 33 项全部通过，5.413 秒。
- 最终交付包沿用 R11 的构建基线，仅增加本轮气泡源码。使用 PyInstaller 6.22.3 执行 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/build-exe.ps1 -AppName Lanxi-Glass-R12`，约 59.83 秒。包为 61,434,009 字节，复制前后 SHA-256 一致。运行 `python build-exe/verify-lanxi-launch.py Lanxi-Glass-R12.exe`，在 `lanxi-folder-package-tx00spc_` 中于 1.84 秒验证独立原生窗口和 SQLite。未测真实模型会话。

## 2026-11-18 列表选中外框与展开箭头

- 以 R10 的任务树和吉光片羽界面为基线。去掉任务、项目组和片羽条目的焦点外框，以及片羽分组入口的悬停和按下反馈。任务树的展开箭头独立于行底色绘制。第一次复测发现选中时底色仍延伸到箭头区，第二次复测发现原生图元在独立绘制时没有显示。最终将行背景裁到条目区域，直接绘制等尺寸三角形。
- Windows 11 x64、Python 3.14.7、PySide6 6.12.0。使用虚构项目、任务及临时笔记库，在 `build-exe/verification/lanxi-flat-lists/r10-release` 设置 `QT_QPA_PLATFORM=windows`，运行 `python tests/smoke_desktop.py --output ../windows-r11`，约 9.43 秒通过。项目组的箭头区域在静止、悬停和选中时逐像素一致。展开与折叠可点，方向随状态变化。验证任务行、待办条目的焦点变化不改变选中外观。截图位于 `windows-r11`。
- 在 `a096c6d` 的集成工作树以 `QT_QPA_PLATFORM=offscreen` 运行 `python -m unittest discover -s tests -t . -p 'test_*.py' -q`，173 项中 172 项通过、1 项跳过，用时 130.730 秒，日志位于 `regression.txt`。最终交付包在 R10 基线 `ff3f703` 只覆盖本次列表源码，以 PyInstaller 6.22.3 运行 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/build-exe.ps1 -AppName Lanxi-Glass-R11`，约 37.35 秒。包为 61,433,557 字节，复制前后 SHA-256 一致，归档确认含新版桌面模块。R10 正在运行，验收使用原生源码窗口，R11 EXE 独立启动未测。

## 2026-11-18 拾得片羽与待理诸事

- 以 `1bd5157` 的 R9 界面和确认过的两张概念图为基线。左栏保留两个固定入口，切换时同步更新列表和右栏组名。沿用 SQLite 表结构。新记录只写正文，编辑旧记录时在同一编辑区读回原题名和正文。待办的完成操作只更新状态，排序和时间戳保持稳定。
- Windows 11 x64、Python 3.14.7、PySide6 6.12.0。在 `build-exe/verification/lanxi-notebook-groups/worktree` 设置 `QT_QPA_PLATFORM=windows`，执行 `python tests/smoke_desktop.py --output ../windows-first`，约 9.16 秒通过。用临时 SQLite 记录覆盖旧随记、旧归档、旧待办的读取和恢复，核对双栏位置、各组新建、单句保存、切换工作区保存、点击勾选和空格取消。1180×760 实机图为 `windows-first/notebook-desktop.png` 和 `windows-first/todo-desktop.png`。未测真实用户笔记库。
- 设置 `QT_QPA_PLATFORM=offscreen` 后运行 `python -m unittest discover -s tests -t . -p 'test_*.py' -q`，164 项中 163 项通过、1 项跳过，用时 72.583 秒。使用 PyInstaller 6.22.3 执行 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/build-exe.ps1 -AppName Lanxi-Glass-R10`，构建约 45.66 秒。交付文件 `release/Lanxi-Glass-R10.exe` 为 61,430,155 字节，复制前后 SHA-256 一致，包内核对了笔记、诗词、毛玻璃及 Windows Qt 插件。R9 正在本机运行，R10 的独立 EXE 窗口未测，实机界面验收使用源码窗口。

## 2026-11-18 吉光片羽双栏毛玻璃与整点换诗

- 以 `b3b8dbf` 的 Codex 页为界面基线，用虚构任务、随记和待办对照。吉光片羽复用两块毛玻璃、列表高亮、标题字重和按钮，自动保存延迟为 450 毫秒。初版截图的筛选选中态区分较弱，复验版降低未选按钮底色并加粗当前分类。两块面板与 Codex 页的位置和尺寸逐项相同。
- Windows 11 x64、Python 3.14.7、PySide6 6.12.0。在 `build-exe/verification/lanxi-notebook-matching/worktree` 设置 `QT_QPA_PLATFORM=windows`，执行 `python tests/smoke_desktop.py --output ../windows-hourly-poetry`，约 9.07 秒通过。保留早期截图 `windows`、筛选复验 `windows-final` 和固定诗句版 `windows-release`。用模拟时间走完 25 联并验证整点换句、出处更新、同小时稳定，以及 1020×640 下不裁字。随记和待办以临时 SQLite 验证新增、自动保存、归档、恢复、完成与切回 Codex 时保留草稿。未等待自然整点或触发真实休眠。
- 选诗依据 [chinese-poetry《唐诗三百首》](https://github.com/chinese-poetry/chinese-poetry/blob/master/全唐诗/唐诗三百首.json)，简体篇名有通用别名，另核对 [常建原文](https://zh.wikisource.org/wiki/破山寺後禪院) 与 [钱起原文](https://ctext.org/text.pl?if=gb&node=153790)。源数据留在验收目录，运行时仅使用随包的 25 联。
- 运行 `python -m unittest discover -s tests -t . -p 'test_*.py' -q`，打包基线集成回归 147 项中 146 项通过、1 项跳过，用时 33.867 秒。执行 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/build-exe.ps1 -AppName Lanxi-Glass-R9`，PyInstaller 6.22.3 构建最终包约 36.69 秒。单独存放前序构建 `package-first` 和 `package-notebook`。交付文件 `release/Lanxi-Glass-R9.exe` 为 61,422,902 字节，包含诗词、随记和 Qt Windows 插件。使用 `python build-exe/verify-lanxi-launch.py Lanxi-Glass-R9.exe` 在 `lanxi-folder-package-kvwtc0zh` 中于 1.43 秒验证原生窗口和 SQLite。
- 源码合并并行的后台提交 `26c0d7e` 后重跑 150 项。首次用时 38.580 秒，Claude 暂停测试清理临时数据库时遇到 Windows 文件占用，输出在 `integrated-regression.txt`。同命令复验用时 38.147 秒，149 项通过、1 项跳过，输出在 `integrated-regression-rerun.txt`。

## 2026-11-18 自动封存、签名发布和预备目录恢复

- 第一阶段的手动封存、复制和恢复作为基线。新增后台定期扫描、按内容去重、签名发布、接收滞后重试和独立预备目录。输入沿用临时 Git 仓库、同一文件的暂存与未暂存两版、未推送提交、选定新增文件及隔离的 Codex 会话目录。Windows、Python 3.14.7、Git 2.56.0.windows.4、Codex CLI 0.169.1，回环模型回复为固定测试文本。
- `.\.venv\Scripts\python.exe -m unittest -v tests.test_handoff tests.test_web` 运行 18 项全部通过，25.456 秒。覆盖重复扫描、同一轮次后的已保存变更生成新版本、清单先到、包截断、清单篡改、合法签名但包身份不同、回退清单以及本机存在分叉会话。验证源仓库、目标 Git 基线、现用工作区和原会话均保留。
- 执行 `.\.venv\Scripts\python.exe tests/smoke_handoff.py --sync --output build-exe/verification/handoff-sync-native-01`，5.24 秒通过。第二次输出保存于 `build-exe/verification/handoff-sync-native-02`，再次通过。测试用 C 盘和 D 盘的独立交换目录模拟文件复制，两次均用真实 Codex CLI 续写原线程，核对目标目录、暂存内容与新回复。结果、接收侧清单、交接包与继续封存包均保留在相应输出目录。
- 本轮验证覆盖自动封存和预备目录恢复。保留已用工作区合并与本机旧会话快进，真实 Syncthing、两台物理机和两种 IDE 交接待验。未测量网络速度和大包吞吐。配置及接口见 [代码与会话交接](../HANDOFF.md)。

## 2026-11-18 封面诗句与三栏工作区

- 以 R6 为基线，对照开发方案头图。诗句从 34 pt 调至 42 pt 粗宋体，保留单行，下方补出处与方案封面的两行副标题。顶部滑块新增「吉光片羽」作为第三个停靠点。随记和待办进入主窗口，切回客户端前提交待保存编辑。
- Windows 11 x64、Python 3.14.7、PySide6 6.12.0。在独立交付源码 `build-exe/verification/lanxi-verse-notebook-release/source/mobile-monitor` 设置 `QT_QPA_PLATFORM=windows`，运行 `python tests/smoke_desktop.py --output ../../windows`。核对 1020×640 和 1180×760 截图、出处和完整副标题、滑块停靠、第三栏及返回后的草稿保存。独立源码回归 `python -m unittest discover -s tests -t . -p 'test_*.py' -q` 运行 121 项，120 项通过、1 项跳过，12.029 秒。
- 通过 PyInstaller 6.22.3 执行 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/build-exe.ps1 -AppName Lanxi-Glass-R7`，交付文件为 `release/Lanxi-Glass-R7.exe`，54,104,555 字节；R6 为 54,103,534 字节。核对包内滑块、随记、桌面和 Windows 平台模块。运行 `python build-exe/verify-lanxi-launch.py Lanxi-Glass-R7.exe`，在 `lanxi-folder-package-9jmzedhj` 中于 1.11 秒验证原生窗口和 SQLite 控制库。

## 2026-11-18 VS Code 与 PyCharm 活动上报

- 目标为将第二里程碑的活动接口接到编辑器，并区分正常退出、断网与意外退出。基线 `b00130d` 可用 CLI 报到和查询协调状态，未提供编辑器插件。环境为 Windows、Python 3.14.7、Node.js 26.13.1、VS Code 1.146.0 与 PyCharm 2026.3（PY-263.7307.206，JBR javac 25.0.4.1）。输入为临时项目、一台虚构设备及回环协调服务。
- 首轮 Python 专项共 17 项，1 项在临时目录清理时失败。测试自身的 SQLite 查询未关闭连接，Windows 返回文件占用。确认连接关闭后，扩展到 18 项全部通过，2.338 秒。专项覆盖正常关闭后只释放当前窗口、异常输入 EOF 保留占用、心跳不推进项目控制版本、开窗回执丢失且断网时将关闭事件落盘补发。回环实测用两个独立的 Python 上报进程分别模拟 VS Code 和 PyCharm。
- 用 `node --test tests/test_vscode_activity.cjs` 验证 VS Code 适配器在启动、多根工作区变更和停用时生成相应的 reporter 生命周期。通过 1 项，Node 报告总耗时约 57 ms，输入为模拟 VS Code API。
- 执行 `.\.venv\Scripts\python.exe scripts/build-ide-adapters.py --pycharm 'C:/Program Files/JetBrains/PyCharm 2026.3' --output build-exe/verification/ide-adapters-02`。两次构建分别用时约 1.27 和 1.45 秒，保留 `ide-adapters-01` 和 `ide-adapters-02`。最后一次 VS Code VSIX 为 2,246 字节，通过 VS Code CLI 安装到该目录下隔离的用户和扩展配置。PyCharm ZIP 为 2,629 字节，使用本机平台库编译并校验 JAR、类文件与清单。编译器提示 `projectOpened` 已弃用，适配器当前限定构建 263。安装输出还有既有 Crashpad 权限提示，CLI 退出码为 0。
- 全量回归使用 `.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p 'test_*.py' -v`，143 项中 142 项通过，1 项可选 Stream SDK 跳过，31.014 秒。输出位于 `build-exe/verification/ide-adapters-regression.txt`。
- 现场待验收包括真实 IDE 窗口生命周期、普通工作区首次打开间隙、真实网络分区和三台物理机。没有测量网络时延或编辑器性能影响。更新后的七阶段口径见 [跨设备方案交付进度](../DELIVERY_STATUS.md)。

## 2026-11-18 项目会话分组与吉光片羽

- 在 R4 界面基线上增设案头项目树，同一 Git 根目录下的会话入同组，其他项目按实际工作目录归组。组内以现有会话标题及本地 Codex 用户消息命名，手动标题优先。同名项目显示父路径。只计算本机历史命名覆盖，4 段记录为 `codexDesk` 的 Codex 会话均能取得已有标题或首条话题。
- 「吉光片羽」从案头底部或托盘打开，含随记与待办。本机 `lanxi-notebook.sqlite3` 保存两种记录。输入后 450 毫秒自动保存，关闭窗口时提交尚未保存的编辑。Qt 测试验证创建、归档、恢复、待办完成及再次打开数据库读回。
- Windows 11 x64、Python 3.14.7、PySide6 6.12.0。独立交付源码目录为 `build-exe/verification/lanxi-project-notebook-release/delivery-source/mobile-monitor`，以 R5 的 `9f4ff0d` 为快照叠加本次桌面文件。设置 `QT_QPA_PLATFORM=windows` 后运行 `python tests/smoke_desktop.py --output ../../windows-delivery`。早期项目树截图在缩进处出现多余高亮，最终限制子行绘制区域后复查消失。R4 与 R6 的顶部滑块及诗句截图逐像素一致，右栏差异仅在悬停工具栏的渲染像素范围内。
- 执行 `python -m unittest discover -s tests -t . -p 'test_*.py' -q`，独立交付源运行 121 项，120 项通过、1 项跳过，11.857 秒。与并行后端工作区的联合回归运行 142 项，141 项通过、1 项跳过，29.622 秒。
- 在独立源码目录用 PyInstaller 6.22.3 执行 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/build-exe.ps1 -AppName Lanxi-Glass-R6`。交付 `release/Lanxi-Glass-R6.exe` 为 54,103,534 字节，R5 为 54,088,903 字节。归档核对包含项目树、笔记与原生桌面模块。使用 `python build-exe/verify-lanxi-launch.py Lanxi-Glass-R6.exe` 验证隔离副本，在 `build-exe/verification/lanxi-folder-package-l0oc1bfc` 于 1.32 秒检测到窗口和控制库。启动验证使用独立目录，实际模型调用与跨设备联调未执行。

## 2026-11-18 跨设备项目控制与桌面共用后台

- 目的为验证人工活动能撤销下一次后台写入许可，且原生写入锁在交还后可重新取得。上一阶段 `b707345` 完成跨盘会话续接，回归 128 项、通过 127 项、跳过 1 项。此次实现中心 SQLite 事务、设备身份验证、活动序号和项目版本，接入 Codex 原生发送边界。试验在 Windows、Python 3.14.7、Codex CLI 0.169.1 下进行。
- 初次用两份 Codex app-server 和临时会话探测 `thread/unsubscribe`，因缺少测试模型提供方配置而接入失败，C 盘临时目录清理还遇到 Windows 文件占用。把原测试配置复制到独立目录后，`ownership-native-03` 证实旧连接收到 `unsubscribed`，第二份原生连接仍被写入锁拒绝。复验 `ownership-native-04` 在 0.2 秒、再等 1 秒和再等 3 秒后重试，仍均拒绝。实现随后改为确认空闲并退出原生进程，再释放中心记录。
- 首轮协调与额度专项共 32 项，其中旧回归用 `__new__` 构造 RPC 导致缺少默认协调字段，1 项报错；补默认字段后 37 项均通过，2.805 秒。PowerShell 裸命令续接和显式参数透传用例独立通过，2.369 秒。专项还覆盖 3 设备并发登记、重复事件、多窗口、中心重开后保留失联占用、旧审批拒绝、发送超时保留写入记录以及明确拒发后的调度重试。
- 使用 `.\.venv\Scripts\python.exe tests/smoke_handoff.py --output build-exe/verification/handoff-v2-regression` 创建独立的跨盘验收数据，约 4.08 秒通过。再运行 `.\.venv\Scripts\python.exe tests/smoke_coordination.py --fixture build-exe/verification/handoff-v2-regression --output build-exe/verification/coordination-native-04`。单机回环使用 3 个虚构设备身份、3 次真实 app-server 连接和 1 条完成的原生会话；验证后台按回执加载目标目录、人工报到后拒发旧后台的新轮次、旧连接退出后人工接手、最后一个人工会话关闭后后台重接。四次通过的协调验证分别记录 1.438、0.873、0.927 和 0.715 秒，未测线上时延。最后一次结果位于 `build-exe/verification/coordination-native-04/result.json`，后台重接后的项目版本为 7。
- 用 `.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p 'test_*.py' -v` 跑本机工作树两次，均运行 139 项，138 项通过，1 项可选 Stream SDK 跳过，分别用时 26.493 和 25.841 秒。最终输出位于 `build-exe/verification/coordination-regression-02.txt`。其中用真实回环 HTTP 验证桌面内嵌 Web 登录、读取同进程 Worker 状态及收尾退出。
- 已完成协议、终端适配器和工作进程接入。VS Code、PyCharm 自动上报，独立网络故障，Claude Code，三台物理设备以及本次源码的 EXE 打包仍待验收。配置、恢复口径与复现命令见 [跨设备项目控制](../PROJECT_CONTROL.md)。

## 2026-11-18 以 R4 为基线重建 R5

- 在 `build-exe/verification/lanxi-r4-baseline/r5-source/mobile-monitor` 解包 R4 源码 `287a3f6`。只覆盖 `9f4ff0d` 的 `desktop_qt.py` 和配套桌面验收脚本。第一轮发现 Git 归档受 CRLF 设置影响，第二轮关闭归档的 CRLF 转换，逐字节核对桌面入口、毛玻璃、配色、后台模块、图标和构建脚本均来自 R4。重建使用已提交的快照。
- Windows 11 x64、Python 3.14.7、PySide6 6.12.0。设置 `QT_QPA_PLATFORM=windows` 后，从该独立源码目录运行 `python tests/smoke_desktop.py --output ../../r5-windows`。同尺寸 1180×760 的 R4 与 R5 实机截图相差 3018 个像素，全部位于左侧任务标题与状态或右侧当前标题区域。其余区域相差 0 像素。运行 `python -m unittest discover -s tests -t . -p 'test_*.py' -q`，118 项通过，1 项跳过，10.976 秒。
- 用 PyInstaller 6.22.3 从独立源码目录运行 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/build-exe.ps1 -AppName Lanxi-Glass-R5`。交付文件 `release/Lanxi-Glass-R5.exe` 为 54,088,903 字节，同构建文件哈希一致，包内已核对 Qt Windows 插件、原生桌面模块及三张壁纸。隔离启动返回 0 时，发现 R4 仍占用单实例锁，未进行第二实例独立启动验收。

## 2026-11-18 跨设备后端第一阶段：Git 与原生 CLI 跨盘续接

- 目的为验证方案的第一个技术依赖：目标工作区能同时保留未推送提交、暂存版、未暂存版、选定新增文件和指定的已完成轮次，并在新路径续写原线程。基线 `9f4ff0d` 的桌面回归为 119 项、通过 118 项、跳过 1 项，尚无交接入口。实施期间 17:00 例行备份产生 `ef577f8`，该提交保存的是开发中的版本。
- Windows 单机，Python 3.14.7、Git 2.56.0.windows.4、Codex CLI 0.169.1。源仓库与源端 Codex 数据目录位于 C 盘临时目录，目标仓库与目标端 Codex 数据目录位于 D 盘。两个 CLI 进程共用一个回环 Responses 测试服务；测试服务在第二次请求带有第一次回复时才返回 `reply-on-B`。输入是一份新建的 Git 仓库、两次固定提示和两份固定回复。
- 复现命令为 `.\.venv\Scripts\python.exe tests/smoke_handoff.py --output build-exe/verification/handoff-native-04`，复测时更换目录名。首次运行输出到 `handoff-native-01`，4.38 秒，退出码 1。核对原生记录发现第二轮已有正确回复，断言读取了旧版 `agent_message` 字段。改为按实际完成事件的 `turn_id` 和 `last_agent_message` 核对后，`handoff-native-02` 通过，5.64 秒；加入回执接口后 `handoff-native-03` 再次通过，3.47 秒。
- 最后一次 `handoff-native-04` 用时 4.11 秒，退出码 0。核对目标 HEAD、Git 状态、二进制文件的暂存内容与工作内容、选定新增文件、新一轮实际 cwd，以及本机恢复后再次封存。线程 `01a1edea-af64-7061-9f60-9ecc7078c31b` 始终一致；原轮次、新轮次、Git 版本和包 SHA-256 均在 `build-exe/verification/handoff-native-04/result.json`。初次封存包 13,731 字节，续接后封存包 14,493 字节。耗时为各次本机命令墙钟时间，部分与回归并行。
- 回归命令为 `.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p 'test_*.py' -v`。首批 5 个专项用例通过，7.594 秒；扩展实现期间专项复测 14.197 秒，初期全套 124 项、跳过 1 项、19.444 秒。增加 PowerShell 裸命令/显式参数透传、子目录映射、历史敏感文件与 Web 鉴权等覆盖后，全套两次均为 128 项、127 项通过、1 项跳过，分别为 25.189 秒和 25.152 秒。跳过的是可选 Stream SDK 回调用例。末次日志在 `build-exe/verification/handoff-regression-04.txt`，仍有既有的 SQLite 与 TestClient 警告。
- 此次验证了实际 CLI 的历史续接、跨盘目录映射、数据校验和冲突保全。封存、传包与首次恢复为显式操作。两台物理机、VS Code/PyCharm 交互式终端、线上模型、后台自动传输与跨机写入权尚未验收，日常额外操作数和网络耗时未测量。使用与接口见 [代码与会话交接](../HANDOFF.md)。

## 2026-11-18 项目文件夹名称与连接状态

- 以标题对齐版为基线。左侧列表、右侧标题和审批窗口共用项目名，优先保留用户手动设置的显示名，默认取工作目录的末级文件夹。用 Windows 目录末尾带反斜杠的记录验证提取，用无目录的历史会话验证标题回退。用完整路径可区分同名任务。
- 列表显示「待执舟」「执舟中」「已执舟」与「已暂泊 · 待续航」，对应现有任务操作。Windows 11 x64、Python 3.14.7、PySide6 6.12.0。设置 `QT_QPA_PLATFORM=windows` 后执行 `python tests/smoke_desktop.py --output build-exe/verification/lanxi-folder-names`，通过并核对实机截图。执行 `python -m unittest discover -s tests -t . -p 'test_*.py' -q`，119 项中 118 项通过、1 项跳过，10.926 秒。
- 用 PyInstaller 6.22.3 执行 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/build-exe.ps1`，更新 `release/Lanxi.exe`，54,088,554 字节，前一版 R4 为 54,086,051 字节。第一次隔离验证因 Windows 环境变量转为普通字典后键名大小写不匹配，在启动前退出。改用 `SYSTEMROOT` 后，`python build-exe/verify-lanxi-launch.py` 在 `build-exe/verification/lanxi-folder-package-2hfndfvz` 内于 1.01 秒发现同路径的原生窗口和 SQLite 控制库。隔离验证未连接真实 CLI。
- GitHub 仓库改为 `gjh1999/mobile-monitor`。REST API 核对改名前后仓库 ID 均为 1461100100，新地址读取到同一初始分支，已同步本地 `origin` 与全局 Git 备份登记。[GitHub 仓库](https://github.com/gjh1999/mobile-monitor)

## 2026-11-18 案头与对谈标题对齐

- 以扁平玻璃版为基线。左侧「案头诸事」由 12 pt 粗体改为与右侧当前任务标题共用 13 pt 粗体。两侧使用相同的上边距、标题行高和垂直对齐。「一处开始」由 12 pt 常规改为 12 pt 半粗。
- 第一次 Windows 验收发现，左侧在右边输入框继承样式前读取了行高。改为两栏完成装配后取值。离屏复测 1020×640、1180×760、1460×1076，标题行同为 y=256、行高均为 34 个逻辑像素。设置 `QT_QPA_PLATFORM=windows` 后运行 `python tests/smoke_desktop.py --output build-exe/verification/lanxi-title-alignment-final`，验证相同字体、相同位置并检查实机截图。
- Windows 11 x64、Python 3.14.7、PySide6 6.12.0。执行 `python -m unittest discover -s tests -t . -p 'test_*.py' -q`，119 项中 118 项通过、1 项跳过，12.055 秒。用 PyInstaller 6.22.3 运行 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/build-exe.ps1 -AppName Lanxi-Glass-R4`，并列包为 `release/Lanxi-Glass-R4.exe`，54,086,051 字节，前一版为 54,086,517 字节。已核对归档中的桌面模块、Windows 插件和三张壁纸，验收期间上一版仍在运行。

## 2026-11-18 扁平玻璃滑块与字号调整

- 以顶部玻璃复核版为基线。「一处开始」小字从 10 pt 提至 12 pt，计数标签从 9 pt 粗体改为 8.5 pt 常规，数字从 17 pt 粗体改为 15 pt 常规。逐帧绘制圆角按压与键盘焦点，移除原生按钮样式画出的方框。胶囊材质取消阴影和高光渐变，滑块以 146/255 的浅色覆盖同一张柔化壁纸。
- Windows 11 x64、Python 3.14.7、PySide6 6.12.0。设置 `QT_QPA_PLATFORM=windows`，执行 `python tests/smoke_desktop.py --output build-exe/verification/lanxi-flat-glass`。检查计数字号与字重、滑块底座和移动块的壁纸透射像素，并复测拖动、反转和减弱动态效果。人工核对静态工作台与拖动截图，动画实际帧率未测。
- 同环境执行 `python -m unittest discover -s tests -t . -p 'test_*.py' -q`，119 项中 118 项通过、1 项跳过，10.895 秒。用 PyInstaller 6.22.3 运行 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/build-exe.ps1 -AppName Lanxi-Glass-R3`，生成 `release/Lanxi-Glass-R3.exe`，54,086,517 字节，前一版为 54,087,019 字节。已检查包内 Qt 平台插件、桌面模块与壁纸。上一版运行期间完成源码窗口和归档验证。

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
