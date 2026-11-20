# 个人设备自动接续

首次配对并登记项目后，笔记本保存代码，桌面端就会在后台取得这次修改。桌面端没有项目时，澜汐从 GitHub 克隆一份。两端可以使用不同盘符和项目目录名。每个项目的身份由 GitHub 的 `owner/repo` 确定，项目记忆在各机使用同一个相对路径。

| 同一项目的文件 | 笔记本 | 台式机 |
|---|---|---|
| 代码 | `D:\Work\客户甲\A\src\main.py` | `E:\Projects\客户甲\A\src\main.py` |
| 默认项目记忆 | `D:\Work\客户甲\A\.codex\memories\decision.md` | `E:\Projects\客户甲\A\.codex\memories\decision.md` |
| 设置项目记忆目录为 `notes/Codex` | `D:\Work\客户甲\A\notes\Codex\decision.md` | `E:\Projects\客户甲\A\notes\Codex\decision.md` |

## 两台电脑首次配对

独立安装包附带可双击运行的本机自检和双机验收，使用测试仓库独立验证。 [安装与验收](PORTABLE_CHECK.md)

1. 在各机运行个人版安装包中的 `Lanxi-Glass-R21-Portable.exe`。使用同一人的 GitHub 仓库权限，各机的 Git 登录和提交身份需可用。
2. 首次打开澜汐时输入配对码。已配对的电脑从托盘「跨机接续 → 邀请另一台电脑」生成配对码。在已有配置的设备上使用同一向导可重新配对。 [云端配对](CLOUD_COORDINATOR.md#在澜汐中配对)
3. 两台电脑各自选择项目母目录。来源设备自动发现其中已有 GitHub origin 的项目，发布仓库标识和母目录下的相对路径。接收机在自己的母目录下按相同路径克隆。已有本机项目映射或旧工作区继续使用；历史清单缺少相对路径时会以 `<owner>/<repo>` 作为后备位置。尚未选择母目录时使用本机 `checkoutRoot` 或用户目录下的 `LanxiProjects`。托盘列出各项目的状态。

平时只需让澜汐在托盘运行。后台最多并行检查四个仓库，每轮完成后默认等待 10 秒。编辑器保存、GitHub 网页提交或任一设备推送后，其他在线设备会在后续检查收到改动。休眠、断网、Git 身份不可用时，恢复后自动重试。代码经 GitHub 传输，个人通道传递项目清单和记忆。只要本机已有项目清单，个人通道短暂离线时仍可同步代码。

仅需同步已在各机登记的仓库时，可以选择「仅启用已登记项目的 GitHub 同步」。自动发现新项目、全局规则和会话续接需要完成个人通道配对。

## 使用现有澜汐协调服务

两台电脑可通过同一个 HTTPS 地址接续。在阿里云或其他 Linux 服务器上可使用仓库里的 Compose 配置和设备登记工具部署。 [澜汐云协调服务](CLOUD_COORDINATOR.md)

按[项目控制说明](PROJECT_CONTROL.md)启动协调服务。在服务端的设备登记中，为同一人的两台设备填写相同的 `personalSpace` 和各自独立的随机 `token`。每个令牌至少 32 字节。只有登记在该个人空间的设备可以读写其项目清单、记忆和会话。服务端示例中的令牌需替换。

```json
{
  "laptop": {"token": "<笔记本设备令牌>", "projects": [], "personalSpace": "my-devices"},
  "desktop": {"token": "<台式机设备令牌>", "projects": [], "personalSpace": "my-devices"}
}
```

客户端 `local-config.json` 中合并以下内容，并在各台电脑设置环境变量 `LANXI_DEVICE_TOKEN`。台式机使用自己的设备名与令牌。跨机服务使用 HTTPS。纯个人接续允许 `projects` 为空。

```json
{
  "coordination": {
    "url": "https://lanxi.example",
    "device": "laptop",
    "projects": {}
  },
  "personalSync": {}
}
```

## 项目目录与分支

从托盘选择配对方式后，R21 将设置保存到 `%LOCALAPPDATA%\Lanxi\data\personal-sync.json`，可从托盘打开该数据目录。也可以在 `local-config.json` 的 `personalSync` 中提供设置，托盘保存的同名键优先。在来源设备上可登记项目已有的记忆相对路径，接收机自动采用。以下是来源机 `personal-sync.json` 示例。

```json
{
  "exchange": "\\\\fileserver\\private-lanxi",
  "checkoutRoot": "D:\\LanxiProjects",
  "projects": [
    {
      "remote": "https://github.com/owner/a.git",
      "path": "D:\\Work\\A",
      "branch": "main",
      "memoryPath": "notes/Codex"
    }
  ]
}
```

`checkoutRoot`、`path` 是各机自己的绝对路径。`memoryPath` 使用项目根目录下的相对路径，各层用 `/` 分隔。默认值为 `.codex/memories`。修改已配对项目的相对目录后，其他设备从共享记录恢复到新位置；迁移前的本地文件保留。GitHub 默认分支用于首次克隆，指定 `branch` 时跟随该分支。用户暂时切到其他分支时，后台等待该工作区返回登记分支。

## 保存、更新与大文件

后台常规同步只发布已跟踪或手工登记的大文件清单。接收端在托盘「跨机接续」查看本机缺少、大小不同的文件及上次清点设备。大小相同的文件在清单中视为一致。R28 的按需借用会临时传输所选文件，回传时核对完整 SHA-256。完成后服务器清理临时文件。 [借用与回传](PORTABLE_CHECK.md)

- 后台把符合条件的已保存修改和新源码合成普通 `WIP` 提交，推送到登记分支。删除和重命名也会反映在接收端。工作区包含人工暂存、变基或未解决的 Git 操作时会显示等待，结束后自动重试。编辑器尚未保存的缓冲区留在编辑器中。
- 两端修改不同位置时，能够干净合并的提交自动合并。同一处冲突会保留本地和远端提交，托盘显示冲突。用户解决后后台继续推送。工作区属于别的仓库、克隆位置已有其他文件或已有用户自行设置的稀疏检出时，后台保留原目录并显示原因。
- 新克隆使用 Git 的部分克隆和稀疏检出。大文件的边界为单文件 **100 MiB 及以上**。普通 Git 大对象留在远端，LFS 文件只接收小指针。已有仓库中的大文件保留在本机；远端要替换本机已有的大文件时等待处理。历史里已手工提交超限对象时，自动推送暂停。远端必须声明支持 Git 对象过滤。此次验证使用 Git for Windows 2.56.0 和 Git LFS 3.8.1。 [Git 部分克隆](https://git-scm.com/docs/git-clone) · [稀疏检出](https://git-scm.com/docs/git-sparse-checkout)
- 遵守 Git 忽略规则，自动提交通道还跳过私有配置、常见凭据文件、Git 过滤器处理的文件和项目记忆目录。记忆目录同时写入本机 `.git/info/exclude`。已经手动纳入 Git 历史的文件仍受该仓库的访问权限约束。

## Codex 记忆与会话

| 内容 | 接收端落点 |
|---|---|
| 项目记忆笔记 | `<项目>/<memoryPath>/` 下的原相对路径 |
| 原生长期记忆 | `<项目>/<memoryPath>/.lanxi/long-term/<线程ID>.json`，本机存在兼容的 `memories_1.sqlite` 时按线程导入 |
| 可续接的完整轮次 | `<项目>/<memoryPath>/.lanxi/sessions/<原文件名>`，同时安装到本机 `CODEX_HOME/sessions` 供原生 Codex 续写 |
| 全局规则与长期笔记 | 本机 `CODEX_HOME/AGENTS.md` 和 `CODEX_HOME/memories/`，未设置该变量时使用用户目录中的 `.codex` |

支持 Markdown、纯文本、JSON、JSONL、YAML 和 TOML 记忆笔记。单个同步记录也小于 10 MiB。两台设备同时修改同一记忆会保留双方版本并在托盘提示。会话只接续最后一轮已经完整落盘的历史；原生写入锁被占用时等待重试，历史已经分叉时保留双方。澜汐使用原线程 ID 接续，并将工作目录映射到当前电脑的项目目录。桌面可直接选择该会话。已安装[PowerShell 终端适配器](PROJECT_CONTROL.md)的开发机在相应目录输入 `codex` 会选择最近已准备的会话。

个人通道包含可读的原生对话内容。共享目录 ACL 或协调服务器存储应只向本人授权。个人同步读取选定的笔记、长期记忆字段和已完成会话。登录凭据、整份 SQLite、WAL 及运行中的进程均留在原设备。复验环境及未做的物理机验收见[实施与验收记录](archive/IMPLEMENTATION_LOG.md)。
互动式项目占用与交还见 [跨设备项目控制](PROJECT_CONTROL.md#个人版项目占用)。吉光片羽中的随记、待办、完成状态、归档和手动分组按条同步到已配对设备。两端同时编辑同一字段会出现「冲突副本」，可在吉光片羽中查看。
