# 代码与 Codex 会话交接

第一阶段可把一个已完成的 Codex 轮次与当时的 Git 状态一起交给另一台 Windows 开发机。源端生成一个封存包，目标端先在临时目录校验，再发布为新的工作区。已有项目目录会保留。两端需具备 Git、Python、Codex CLI，各自完成 CLI 登录。图形界面可通过现有 Web 服务查询恢复记录。

## 源端封存

以下 PowerShell 命令从澜汐源码根目录运行。两端用同一个项目标识，使用各自的本机路径。先完成当前轮次并退出源端 CLI。选择两端都持有的提交作为基线，例如已同步的 `origin/main`。输出目录需要预先存在，放在项目工作树之外。

```powershell
$snapshot = .\.venv\Scripts\python.exe handoff.py capture `
  --project vision --repo 'D:\Work\Vision' --base origin/main `
  --thread '替换为原线程的 UUID' --include 'notes/next.md' `
  --out 'D:\LanxiExchange\vision-001.zip' | ConvertFrom-Json
$snapshot
```

包内含基线以后尚未共享的提交历史、暂存区补丁、已修改工作文件的内容及删除状态、逐个 `--include` 的未跟踪文件，以及指定线程的完整原生 JSONL。没有新增文件时省略 `--include`。仅对获准跨设备传输原始会话和这段提交历史的项目使用。把包传到目标端，并通过可信渠道取得封存回执里的 `sha256`。包使用 ZIP 格式，内容为明文。

封存会复查 Git 状态、工作文件内容和会话记录。中途变化、末轮没有完成标记、未解决冲突或选中了忽略文件时会报错。单个工作文件、会话、Git 对象及暂存补丁上限为 64 MiB，整包对象未压缩上限为 512 MiB。内容过滤器（含 Git LFS）、子模块、稀疏检出、特殊索引标志和在途 Git 合并或变基留待后续支持。常见凭据文件和运行目录会被拒绝，源端仍需确认项目的数据授权范围。

## 目标端恢复并续接

`--seed` 指向目标端已有共同提交的本地仓库。`--dest` 是准备打开的新项目目录，父目录须存在。目标端从 seed 读取共同提交和已配置的 origin，校验包摘要、对象、索引与工作区状态后才发布目录。

```powershell
.\.venv\Scripts\python.exe handoff.py restore `
  --project vision --seed 'C:\Code\Vision' --dest 'C:\Code\Vision-continued' `
  --package 'C:\LanxiExchange\vision-001.zip' --sha256 '替换为源端给出的 64 位摘要'
```

在 VS Code 或 PyCharm 的 PowerShell 终端配置中加载一次适配器。下面的第一行也可用于当前终端临时加载，从任意目录执行时填写澜汐源码的实际绝对路径。

```powershell
. 'D:\VscodeProject\codexDesk\mobile-monitor\scripts\lanxi-terminal.ps1'
Set-Location -LiteralPath 'C:\Code\Vision-continued'
codex
```

仅在已恢复的仓库中，裸命令 `codex` 才会准备会话并调用原生 `codex resume --cd <映射后的轮次目录> <原线程 ID>`。显式参数直接传给原生 CLI，在当前目录主动开新会话可用 `codex --cd .`。执行 `handoff.py prepare-resume --cwd <目录>` 可单独完成准备并输出线程和目录。它把封存的单个线程放到当前用户的 Codex 会话目录，遵从 `CODEX_HOME`。发现已归档、重复或分叉的同 ID 记录时会报错并保留现场。已有记录包含同一封存前缀时，保留本机后来追加的轮次。

上面是手动流程。首次恢复使用新目录，后续交接包若与现有线程历史冲突，需要核对两端记录。

## 使用同步目录自动封存和预备

只登记允许把原始会话和 Git 状态跨机保存的项目。在两端准备由 Syncthing 等工具配对的专用交换目录，项目仓库、本机配置、状态文件和预备工作区留在这两个交换目录之外。两端使用同项目标识和同一个至少 32 字节的随机密钥，通过 `keyEnv` 指定密钥环境变量的名称。分别使用不同的设备标识。项目和设备标识使用小写英文、数字、下划线或连字符。

在源端 `local-config.json` 中加入配置，替换线程 UUID、仓库路径和 Git 基线。设置 `include` 时列出允许传输的单个未跟踪文件，可提前列入后续才创建的文件。仓库已跟踪的工作区变化和暂存改动按封存规则处理。

```json
{
  "handoffSync": {
    "device": "office",
    "projects": {
      "vision": {
        "exchange": "D:\\LanxiExchange",
        "keyEnv": "LANXI_VISION_HANDOFF_KEY",
        "publish": {
          "repo": "D:\\Work\\Vision",
          "base": "origin/main",
          "thread": "替换为原线程的 UUID",
          "include": ["notes/next.md"]
        }
      }
    }
  }
}
```

目标端示例使用同项目的 `keyEnv`，`seed` 需拥有共同基线提交。`staging` 位于目标机本地，后台会为每个已校验的设备版本创建独立子目录。

```json
{
  "handoffSync": {
    "device": "laptop",
    "projects": {
      "vision": {
        "exchange": "C:\\LanxiExchange",
        "keyEnv": "LANXI_VISION_HANDOFF_KEY",
        "receive": {
          "seed": "C:\\Code\\Vision",
          "staging": "C:\\LanxiStaging",
          "peers": ["office"]
        }
      }
    }
  }
}
```

启动桌面或 Web 后台后自动扫描，默认间隔 15 秒。可在 `handoffSync.scanSeconds` 设置间隔，最短 5 秒。用 `python handoff_sync.py --config local-config.json` 可在后台未运行时执行单次扫描。

源端只发布已确认落盘的轮次及其封存时的 Git 状态。未改变的输入不会反复发布；同一轮后的新增保存也可以生成新版本。源端先写入按 SHA-256 命名的交接包，再发布带 HMAC 的清单。密钥由登记设备共享，用于验证来源和完整性。包内代码和原始会话为明文，由配对工具负责传输与访问控制。

目标端等清单和包都到达后校验，在本机独立目录预备。已有工作区和较早的预备目录会保留。登录后可从 `GET /api/v1/handoff-sync` 查看各项目的来源、版本与本机预备路径。`ready` 表示包已还原且本机历史通过相容性检查；`session_conflict` 表示预备成功但同线程历史不同，原记录和预备目录均保留；`awaiting_package` 表示只有清单到达；`restore_failed` 表示包或本地基线校验失败，后续扫描会重试；`version_conflict` 表示清单版本回退或同版本内容不同。

在返回的预备目录中使用终端适配器续接。当前自动化覆盖封存、发布和预备，打开正确的预备目录仍由操作者决定。合并现用工作树或将较短的本机同线程历史快进到远端进度，需在独立联调后接入。

## 前端查询

在现有 Web 服务的 `local-config.json` 中登记需要展示的项目与本机恢复目录，例如：

```json
{"handoffProjects": {"vision": "C:\\Code\\Vision-continued"}}
```

登录后读取 `GET /api/v1/handoffs`。响应的 `projects` 数组按登记项返回。状态 `restored` 带有已恢复快照的 `head`、`thread`、`turn` 和包的 `sha256`。`awaiting_restore` 表示尚未发现回执，`invalid_receipt` 表示映射或回执异常。未配置项目时数组为空。接口只读取 `.git/lanxi/handoff.json` 的摘要字段；状态描述上次恢复结果，实时运行状态仍使用现有任务接口。

## 本机跨盘复验

```powershell
.\.venv\Scripts\python.exe -m unittest -v tests.test_handoff tests.test_web
.\.venv\Scripts\python.exe tests/smoke_handoff.py --output build-exe/verification/handoff-local-repeat
.\.venv\Scripts\python.exe tests/smoke_handoff.py --sync --output build-exe/verification/handoff-sync-repeat
```

每次为 smoke 指定新输出目录。它在系统临时目录创建源仓库，在输出目录创建目标仓库，运行本机真实 Codex CLI，使用不同的临时 Codex 数据目录和仅监听回环的回复服务。第二轮仅在收到第一轮历史后返回预期文本。脚本验证 Git 状态、原线程、新轮次和实际工作目录，保存 `result.json`、封存包和目标端现场。双机、两种 IDE、实际模型服务及日常操作次数需另行验收。
