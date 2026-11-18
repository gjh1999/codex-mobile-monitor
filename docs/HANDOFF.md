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

封存会复查 Git 状态、工作文件内容和会话记录。中途变化、末轮没有完成标记、未解决冲突或选中了忽略文件时会报错。单个工作文件、会话、Git 对象及暂存补丁上限为 64 MiB，整包对象未压缩上限为 512 MiB。已登记的 Git LFS 路径通过提交中的指针交接，实际对象由接收端单独预取。其他内容过滤器、子模块、稀疏检出、特殊索引标志和在途 Git 合并或变基留待后续支持。常见凭据文件和运行目录会被拒绝，源端仍需确认项目的数据授权范围。

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

仅在已恢复的仓库中，裸命令 `codex` 才会准备会话并调用原生 `codex resume --cd <映射后的轮次目录> <原线程 ID>`。显式参数直接传给原生 CLI，在当前目录主动开新会话可用 `codex --cd .`。执行 `handoff.py prepare-resume --cwd <目录>` 可单独完成准备并输出线程和目录。它把封存的单个线程放到当前用户的 Codex 会话目录，遵从 `CODEX_HOME`。发现归档、压缩、多份记录或分叉时会报错并保留现场。已有记录包含同一封存前缀时，保留本机后来追加的轮次。

### 往返续接

从另一台机器带回原线程后，如果本机完整历史是新历史的逐字节前缀，终端适配器会在启动原生 CLI 前快进。当前已验证 Windows 的 Codex CLI 0.169.1。使用协调服务的设备先登记该线程 ID，即使预备目录在项目原路径之外，适配器也会先申请对应项目的 CLI 占用。单独执行快进可在 `prepare-resume` 命令加上 `--codex <实际 codex.exe>`。

快进期间持有 Codex 本机协调锁和线程写入锁，重新核对旧历史后原子发布。先将旧版副本保存在 `CODEX_HOME/lanxi-handoff-backups/<线程 ID>/`。发现仍有原生写入者、CLI 版本不匹配或文件刚刚变化时会保留两端数据并提示重试。此版本支持未压缩的原生 JSONL，其他表示形式等待相应兼容性验证。 [Codex CLI 0.169.1 写入锁源码](https://github.com/openai/codex/blob/rust-v0.169.1/codex-rs/rollout/src/writer_lock.rs)

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

目标端等清单和包都到达后校验，在本机独立目录预备。已有工作区和较早的预备目录会保留。登录后可从 `GET /api/v1/handoff-sync` 查看各项目的来源、版本与本机预备路径。`ready` 表示包已还原且本机历史通过相容性检查。`advance_on_resume` 表示本机历史是较早的完整前缀，使用适配器启动时再验证原生锁并快进。`session_conflict` 表示预备成功且现有历史需核对，原记录和预备目录均保留。`awaiting_package` 表示只有清单到达。`restore_failed` 表示包或本地基线校验失败，后续扫描会重试。`version_conflict` 表示清单版本回退或同版本内容不同。

默认通过终端适配器在返回的预备目录中续接。Windows 上也可以为登记项目启用空闲时自动更新原路径。

两端都要续写时，可以在同一项目条目同时配置 `publish` 和 `receive`。`publish.repo` 填本机登记仓库，`publish.thread` 填同一线程。最近完成轮次位于登记的预备区、且该目录回执与项目和线程相符时，下次自动封存会使用该预备工作区。Git 基线仍从登记仓库解析，例如其 `origin/main`。首次配对完成后，各回合之间可以复用同一份配置。

## 合并本机和已接收的工作区

两份工作区有共同提交历史时，可按需生成独立的合并工作区。既有的两个输入目录都保留。`--incoming` 指向带恢复回执的目录。`--dest` 使用尚不存在的目录，父目录需已存在。

```powershell
.\.venv\Scripts\python.exe handoff_merge.py `
  --local 'C:\Code\Vision' --incoming 'C:\LanxiStaging\vision\office-1-示例' `
  --dest 'C:\LanxiStaging\vision\merged-local' --include 'notes/mine.md'
```

本机未跟踪文件用 `--include` 逐个列出。接收端使用恢复回执里的逐文件清单。旧回执没有该字段时，可用 `--include-incoming` 指定。存在祖先关系时采用较新的提交。历史分叉时由 Git 根据共同祖先合并已提交内容，并在预备目录创建一个双父提交，提交者显示为 `Lanxi Workbench <lanxi@localhost>`。本机提交为第一父提交，接收端提交为第二父提交。随后分别计算暂存区和未暂存层，保留未提交修改的状态。成功路径只检出和打包各一次，打包最多使用两个线程。放在已登记预备区下的结果可使用终端适配器续接原线程。 [Git 三方树合并](https://git-scm.com/docs/git-merge-tree) · [Git 共同祖先](https://git-scm.com/docs/git-merge-base) · [Git 构建提交](https://git-scm.com/docs/git-commit-tree)

命令返回版本 1 的 JSON。`history` 记录两端原始 HEAD，找到共同祖先时还包括 `bases`。成功为 `merged`，返回结果 `head`、两个结果树标识、线程和可打开的目录。`history.mode` 为 `same_head`、`local_ahead`、`incoming_ahead` 或 `merge_commit`。

发生冲突时返回 `conflict`、所在层、文件路径和 Git 冲突类型。`history` 层表示已提交内容冲突，`local_index` 或 `incoming_index` 表示一端的暂存区与新提交基线冲突，`index` 表示两端暂存区相互冲突。后续工作层分别报告 `local_worktree`、`incoming_worktree` 或 `worktree`。没有共同历史、不同对象格式或浅克隆遇到不同 HEAD 时，返回 `history_unavailable` 和 `history.reason`，分别为 `unrelated_history`、`object_format` 和 `shallow_history`。这些结果使用退出码 1，配置、校验和运行错误使用退出码 2。

合并只支持普通文件，使用隔离的 Git 配置。最终的暂存区和工作区均会检查内容过滤器及大小写路径冲突。已提交树新带入的路径还会检查私有文件及跨机路径规则。Git 子模块和特殊索引标志留待支持。

## 应用到本机原路径

Windows 上可把上一步的合并结果应用到 `coordination.projects.<项目>.path` 指定的原工作区。使用支持切换占用的协调服务，并通过 `LANXI_DEVICE_TOKEN` 提供设备令牌。原工作区需要独立的 `.git` 目录、当前分支、干净的暂存区和已跟踪文件，且 HEAD 与合并回执的本机提交一致。执行时关闭该项目的 IDE、终端及其他会写入工作区的进程；协调服务检查已登记的占用，未接入活动上报的进程需由操作者确认。[跨设备项目控制](PROJECT_CONTROL.md)

预备目录必须是 `handoffSync.projects.<项目>.receive.staging` 下对应项目目录的直接子目录，且由 `handoff_merge.py` 生成。执行下列命令后，在原路径用已安装的终端适配器打开 `codex`。

```powershell
.\.venv\Scripts\python.exe handoff_apply.py --project vision `
  --prepared 'C:\LanxiStaging\vision\merged-local'
```

命令在协调服务内取得项目的排他占用，检查原生线程写入锁，按需检出受影响文件，并把原索引和被替换的文件保存在原仓库 `.git/lanxi-switch/<切换ID>/`。原仓库的配置、其他分支和无关的未跟踪或忽略文件留在原位置。发生路径碰撞、出现本机已跟踪修改或检测到占用时，停止切换。当前支持普通文件的增删改。文件与目录互换、符号链接、连接工作树和断电恢复仍待验证或实现。 [Git 按路径检出索引](https://git-scm.com/docs/git-checkout-index) · [Git 引用校验与更新](https://git-scm.com/docs/git-update-ref)

成功时输出版本 1 的 JSON，`status` 为 `applied`，附原路径、合并提交、线程、受影响文件数和备份目录。运行错误退出码为 2。文件被占用等中途错误会尝试自动回滚。进程中断或协调服务未确认结束时，先退出占用程序，再用以下命令继续恢复。恢复结果 `rolled_back` 表示回到切换前，`applied` 表示已完成切换且补发了结束确认，`no_pending_switch` 表示当前无等待恢复的切换。如果文件或索引在中断后又被修改，操作会保留现场供核对。切换完成前暂停封存和会话续接。切换后，下一次封存须等本机原生记录包含接收的完整历史。

```powershell
.\.venv\Scripts\python.exe handoff_apply.py --project vision --recover
```

## 空闲时自动应用到原路径

默认关闭。确认所用 IDE、终端和会写工作区的后台都接入同一协调服务后，在 Windows 目标端的 `handoffSync.projects.<项目>.receive` 中增加 `autoApply`。协调配置使用与 `handoffSync.device` 相同的设备标识，项目路径登记为原工作区，`threads` 列出对应原生线程。`peer` 选择 `peers` 内一个受信接收端，`thread` 填实际 UUID，`branch` 使用本机分支短名。配对项目继续使用各自的交接密钥。 [跨设备项目控制](PROJECT_CONTROL.md)

```json
{
  "seed": "C:\\Code\\Vision",
  "staging": "C:\\LanxiStaging",
  "peers": ["office"],
  "autoApply": {"peer": "office", "thread": "替换为登记的线程 UUID", "branch": "main"}
}
```

同步后台先预备快照，再检查项目占用和本机分支。已跟踪文件和暂存区干净时，会复验签名、暂存区和已封存的文件内容，按需合并并沿用原路径切换的占用与回滚流程。提交前再次核对来源版本和本机会话，已有较新会话或分叉时保留双方。应用完成后，下一次自动发布等待本机完成一个新轮次。重复收到已应用的快照时复用本机回执，跳过合并、切换和协调查询。

`GET /api/v1/handoff-sync` 的项目条目含 `autoApply.status` 和 `lastApplied`。`applied` 表示已应用，`occupied`、`local_changes`、`branch_mismatch` 和 `local_session_ahead` 表示等待条件满足。`staging_changed` 表示预备目录的内容和签名包不符，`blocked` 表示应用校验或执行失败，同版本 60 秒后重试。已确认的合并冲突在本机 HEAD 或来源版本改变后重新计算。`recovery_required` 表示切换日志还在，按原路径说明运行 `--recover` 后再恢复自动更新。`already_seen` 表示扫描状态记录了旧应用，但当前工作区回执不同，保留原工作区供核对。移除 `autoApply` 或设为 `false` 后继续常规预备。

## 前端查询

在现有 Web 服务的 `local-config.json` 中登记需要展示的项目与本机恢复目录，例如：

```json
{"handoffProjects": {"vision": "C:\\Code\\Vision-continued"}}
```

登录后读取 `GET /api/v1/handoffs`。响应的 `projects` 数组按登记项返回。状态 `restored` 带有工作区的 `head`、`thread`、`turn` 和接收包的 `sha256`。登记独立合并目录或已完成原路径切换时，`head` 为合并结果的 HEAD。`switch_pending` 表示当前切换尚在执行或等待恢复，`awaiting_restore` 表示尚未发现回执，`invalid_receipt` 表示映射或回执异常。未配置项目时数组为空。接口读取 `.git/lanxi/handoff.json` 的摘要字段。实时运行状态使用现有任务接口。

## 按需预取 Git LFS 对象

家庭端使用 Gitea 的内置 LFS 服务，为获准同步的项目建独立仓库，并使用经授权的 HTTPS 地址。代码仓库继续使用自己的 Git origin。配置本机环境变量 `LANXI_LFS_USER` 和 `LANXI_LFS_TOKEN`，私有仓库令牌使用满足下载权限的账号。端点填写家庭 Gitea 的完整 `仓库.git/info/lfs` 地址。 [Gitea 内置 LFS](https://docs.gitea.com/administration/git-lfs-setup/) · [Git LFS Batch 协议](https://github.com/git-lfs/git-lfs/blob/main/docs/api/batch.md)

从澜汐源码目录调用。`--path` 指定提交中启用了 `filter=lfs` 的普通文件，可重复传入。`--ref` 默认 `HEAD`，执行时解析成固定提交。命令把对象放到目标仓库的默认 `.git/lfs/objects` 缓存。项目位于对应提交并启用了 Git LFS 过滤器时，可用 `git lfs checkout` 从本机缓存检出工作副本。如果环境启用了 `GIT_LFS_SKIP_SMUDGE`，显式检出时先关闭该设置。 [Git LFS 指针规范](https://github.com/git-lfs/git-lfs/blob/main/docs/spec.md) · [Git LFS 离线检出](https://github.com/git-lfs/git-lfs/blob/main/docs/man/git-lfs-checkout.adoc)

```powershell
.\.venv\Scripts\python.exe lfs_assets.py --repo 'C:\Code\Vision' --ref HEAD `
  --path 'models/weights.bin' --endpoint 'https://gitea.example/home/vision.git/info/lfs' `
  --user-env LANXI_LFS_USER --token-env LANXI_LFS_TOKEN

.\.venv\Scripts\python.exe lfs_assets.py --repo 'C:\Code\Vision' --ref HEAD `
  --path 'models/weights.bin' --check

git -C 'C:\Code\Vision' lfs checkout -- 'models/weights.bin'
```

命令按对象去重并分批请求，单次只流式下载一个对象。中断时保留部分文件，重试带上 `Range`，并校验服务返回的偏移。服务器改回完整响应时从头写入。对象大小和 SHA-256 都匹配后才发布到标准缓存。旧缓存校验失败时保留原文件供核对。下载地址须与配置端点同源并指向该仓库的对象路径；跳转和外部对象存储直链留待后续支持。 [Git LFS 基础传输](https://github.com/git-lfs/git-lfs/blob/main/docs/api/basic-transfers.md)

输出为版本 1 的 JSON，包含 `commit`、总状态与按路径索引的 `files`。所有对象校验通过时返回 `ready` 和退出码 0。缺件、断线、散列错误或协议错误时返回 `incomplete` 和退出码 1，各对象提供对应状态。配置或 Git 指针无效使用退出码 2。`--check` 只检查本机缓存。此入口适用独立 Git 仓库的默认缓存。

```powershell
.\.venv\Scripts\python.exe -m unittest -v tests.test_lfs_assets
.\.venv\Scripts\python.exe tests/bench_lfs_assets.py --output build-exe/verification/lfs-repeat
```

## 把必需 LFS 对象纳入交接

在源端 `handoffSync.projects.<项目>.publish` 中增加 `"lfs": ["models/weights.bin"]`，或在手动封存时对每个路径传入 `--lfs`。清单需覆盖当前提交中启用 LFS 的路径。先提交指针，工作区可保留标准指针或与其散列相符的已检出文件。暂存区或工作区中存在 LFS 修改时，封存会保留原版本。交接包和签名清单记录对象标识与大小，实际大文件的下载单独走 Gitea。

目标端在项目的 `receive` 下配置 `lfs`，为允许下载的路径名单、家庭端点和凭据环境变量。`seed` 使用独立 Git 仓库的默认 `.git/lfs` 缓存，同一对象供不同预备版本复用。配置中省略 `lfs` 时，只检查本机的预备与基线缓存。 [Gitea 内置 LFS](https://docs.gitea.com/administration/git-lfs-setup/) · [Git LFS 离线检出](https://github.com/git-lfs/git-lfs/blob/main/docs/man/git-lfs-checkout.adoc)

```json
{
  "lfs": {
    "paths": ["models/weights.bin"],
    "endpoint": "https://gitea.example/home/vision.git/info/lfs",
    "userEnv": "LANXI_LFS_USER",
    "tokenEnv": "LANXI_LFS_TOKEN"
  }
}
```

`GET /api/v1/handoff-sync` 对此类接收结果显示按路径的 `lfs` 状态。对象缺失、断线或坏校验时为 `lfs_pending`，预备目录保留。下载配置或许可路径不匹配时为 `lfs_unconfigured`，预备目录中的 LFS 文件被改动时为 `lfs_staging_changed`。重复扫描可在缓存文件元数据一致时复用本次进程中的校验结果。合并、原路径切换以及原生续接前再次按内容校验必需对象。自动切换后的标准缓存也可使用原生 `git lfs checkout` 检出工作副本。

```powershell
.\.venv\Scripts\python.exe -m unittest -v tests.test_handoff_lfs tests.test_lfs_assets
.\.venv\Scripts\python.exe -m tests.bench_handoff_lfs --output build-exe/verification/handoff-lfs-repeat
```

## 本机跨盘复验

```powershell
.\.venv\Scripts\python.exe -m unittest -v tests.test_handoff tests.test_web
.\.venv\Scripts\python.exe tests/smoke_handoff.py --output build-exe/verification/handoff-local-repeat
.\.venv\Scripts\python.exe tests/smoke_handoff.py --sync --output build-exe/verification/handoff-sync-repeat
.\.venv\Scripts\python.exe tests/smoke_handoff.py --sync --roundtrip --output build-exe/verification/handoff-roundtrip-repeat
.\.venv\Scripts\python.exe tests/smoke_handoff.py --sync --roundtrip --merge --output build-exe/verification/handoff-merge-native-repeat
.\.venv\Scripts\python.exe tests/smoke_handoff.py --sync --roundtrip --merge --diverged --output build-exe/verification/handoff-diverged-native-repeat
.\.venv\Scripts\python.exe tests/smoke_handoff.py --sync --roundtrip --merge --diverged --apply --stock-files 768 --output build-exe/verification/handoff-apply-repeat
.\.venv\Scripts\python.exe tests/smoke_handoff.py --sync --roundtrip --merge --diverged --apply --automatic --stock-files 768 --output build-exe/verification/handoff-auto-repeat
.\.venv\Scripts\python.exe -m unittest -v tests.test_handoff_merge
.\.venv\Scripts\python.exe -m unittest -v tests.test_handoff_apply tests.test_coordination
.\.venv\Scripts\python.exe -m unittest -v tests.test_handoff_auto tests.test_handoff
.\.venv\Scripts\python.exe tests/bench_handoff_merge.py --output build-exe/verification/handoff-merge-repeat
.\.venv\Scripts\python.exe tests/bench_handoff_merge.py --diverged --output build-exe/verification/handoff-diverged-repeat
```

每次为 smoke 指定新输出目录。它在系统临时目录创建源仓库，在输出目录创建目标仓库，运行本机真实 Codex CLI，使用不同的临时 Codex 数据目录和仅监听回环的回复服务。第二轮仅在收到第一轮历史后返回预期文本。脚本验证 Git 状态、原线程、新轮次和实际工作目录，保存 `result.json`、封存包和目标端现场。双机、两种 IDE、实际模型服务及日常操作次数需另行验收。
