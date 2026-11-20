# 澜汐云协调服务

台式机和笔记本分别连到同一个 HTTPS 域名，使用各自的设备令牌。协调服务保存项目清单、Codex 记忆和会话。代码继续从 GitHub 同步。

## 在 Linux 服务器启动

准备一台安装了 Docker Engine 与 Compose 插件的 Linux 服务器，以及一个指向服务器公网 IP 的域名。放行 TCP 80 和 443，便于 Caddy 签发及续期证书。Compose 只发布 Caddy 的端口，Python 服务通过容器网络提供接口。 [Caddy 自动 HTTPS](https://caddyserver.com/docs/automatic-https) · [Docker Compose 安装](https://docs.docker.com/compose/install/)

把澜汐源码或服务器分发目录放到服务器，进入 `deploy/coordinator`。复制配置模板后，在 `.env` 中把 `sync.example.com` 换成自己的域名。

```bash
cd deploy/coordinator
cp .env.example .env
```

```bash
docker compose build
docker compose run --rm --no-deps coordinator cloud_admin.py init desktop laptop
docker compose up -d
```

`init` 为两台设备生成不同的随机令牌，并把它们放入同一个个人空间。执行一次即可。服务启动后，从两台 Windows 电脑各运行一次下列命令。将地址换成自己的域名。返回 `{"status":"ready"}` 表示所在电脑能访问协调服务。

```powershell
Invoke-RestMethod https://sync.example.com/healthz
```

## 配对两台电脑

从服务器分别生成两台电脑的配置。命令输出客户端 JSON 和该设备专用的 PowerShell 令牌设置。

```bash
docker compose exec coordinator python cloud_admin.py pair desktop --url https://sync.example.com
docker compose exec coordinator python cloud_admin.py pair laptop --url https://sync.example.com
```

1. 把各自的 JSON 配置合并到对应电脑的 `local-config.json`。R21 可在托盘选择「打开用户数据目录」，R20 的配置在 EXE 同目录。
2. 在对应电脑的 PowerShell 中执行输出里的两行令牌设置。保存配置后，从托盘退出澜汐并重新启动。可从刚设置令牌的 PowerShell 启动 EXE。
3. 以前用过共享目录或仅代码同步的设备，在同一数据目录的 `personal-sync.json` 把 `exchange` 设为 `null`，`codeOnly` 设为 `false`。如果有 `memory` 字段，将它设为 `true`。

源电脑发布已登记的项目，接收电脑可从「跨机接续」查看项目及克隆状态。各机需具备 GitHub 仓库权限。 [项目登记和本机路径](PERSONAL_SYNC.md#项目目录与分支)

## 双机验收

选一个两台电脑都能访问的测试仓库。在台式机已登记的项目里保存一个小代码文件，再在项目的记忆目录里新建 `check.md`。等笔记本收到两份文件后，从笔记本修改它们，等台式机收到回传。两台电脑可使用不同盘符，记忆保留相同的项目内路径。

验收 Codex 时，在台式机完成一轮对话并记录会话 ID。等笔记本收到同一会话 ID 后，在笔记本的终端恢复该会话。

```powershell
codex resume -C "C:\Projects\A" "会话 ID"
```

## 数据和服务维护

设备登记和 SQLite 位于 `coordinator-data` 持久卷。容器重启和重建会继续使用原数据。Caddy 的证书数据保存在自己的持久卷中。

```bash
docker compose ps
docker compose logs --tail 60 coordinator https
docker compose restart coordinator
```

用 SQLite 的备份 API 可在服务运行期间制作一致副本。将备份目录放在仅授权人员可访问的位置，设备文件含令牌。

```bash
docker compose exec -T coordinator python -c 'import sqlite3; a=sqlite3.connect("/var/lib/lanxi/coordination.sqlite3"); b=sqlite3.connect("/var/lib/lanxi/backup.sqlite3"); a.backup(b); b.close(); a.close()'
mkdir -m 700 backup
docker compose cp coordinator:/var/lib/lanxi/backup.sqlite3 backup/coordination.sqlite3
docker compose cp coordinator:/var/lib/lanxi/devices.json backup/devices.json
```

若要在同一云主机额外备份代码，可另设 GitHub 仓库镜像。代码镜像和协调服务数据库分别维护。
