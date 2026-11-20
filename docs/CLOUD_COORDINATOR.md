# 澜汐云协调服务

台式机和笔记本使用各自的设备令牌访问协调服务。R23 可用一次性配对码连接 HTTPS，私人部署也可使用带服务器证书的公网 IP。协调服务保存项目清单、Codex 记忆和会话。代码继续从 GitHub 同步。

## 在澜汐中连接云服务器

R33 私人版打开时选择「连接云服务器」或「本机使用」。如果还没有项目母目录，再选这台电脑的目录。使用同一私人包的设备各自点击「连接云服务器」，即可先后加入同一工作区。顶部任务栏和主窗口左下角也能连接。本机模式保留已保存的连接凭据，当前运行期间关闭澜汐云同步和云更新。

私人便携包内置工作区加入授权，只在已授权的设备间传递。服务器管理员可用 `cloud_admin.py workspace-grant <已登记设备> --url <HTTPS 地址>` 签发打包授权，私人证书部署另加 `--certificate`。`workspace-revoke <已登记设备>` 可收回该工作区的包内加入授权，已登记设备继续使用自己的凭据。签发结果用于构建私人包。 [构建说明](RUNBOOK.md#构建-exe)

## 在澜汐中配对

旧版或通用构建可使用下列邀请码和设置文件流程。

两台电脑都未配对时，请先由私人协调服务的管理员签发首台设备配对码。先让源头电脑加入，再由它生成第二台的配对码。已经部署好的私人服务可直接执行下文的签发命令。

1. 已配对的电脑从托盘选择「跨机接续 → 邀请另一台电脑」，生成并复制配对码。配对码十分钟内有效，只能由一个新设备领取。
2. 在另一台电脑首次打开澜汐，选择「全新开始」后，在「设置项目母目录」窗口选择本机目录并粘贴配对码，点「连接并同步」。已配置过目录时，点主窗口左下角「配对」，或从托盘选择「跨机接续 → 使用配对码连接」。
3. 在托盘查看「已连接协调服务」。网络恢复后后台会重试。项目会按各自母目录下的相对路径恢复，GitHub 和 Codex CLI 各自沿用本机登录。

配对码带服务地址。私人证书写进配对码，只从可信的已配对设备或自己的服务器管理员接收。设备令牌在本机由 Windows DPAPI 保护。

### 双机自动配对

两台均未配对时，可在 R32 的「配对」窗口各自选择本机项目母目录，再导入同一份 `.lanxi-pairing` 设置文件并点击「申请自动配对」。先申请的电脑会等待，第二台提交后，两台同时加入设置文件指定的私人空间。服务器只接收两台不同的设备，文件自签发起 30 分钟内可用于首次申请。设备继续使用各自的独立凭据。

服务管理员可从协调主机签发文件。以下命令把输出的一行设置凭据保存在本地文件中。只把该文件交给要配对的两台电脑。

```bash
sudo docker compose -f compose.private-https.yaml exec -T personal-https python cloud_admin.py setup desktop --url https://服务器公网IP --certificate /run/lanxi-tls/server.crt > 两台电脑.lanxi-pairing
```

### 服务器管理员为首台设备生成配对码

已有 HTTPS 域名时，用新版协调服务镜像。无域名的私人部署可使用 `compose.private-https.yaml`，将为公网 IP 签发且含 IP SAN 的证书与私钥分别放到 `deploy/coordinator/tls/server.crt` 和 `server.key`。容器使用 UID/GID 10001，给该用户读取私钥的权限。确认 TCP 443 可达。这个入口只提供个人同步和设备配对，沿用 `lanxi-coordinator` 的持久卷。

```bash
sudo docker compose -f compose.private-https.yaml up -d --build --wait personal-https
sudo docker compose -f compose.private-https.yaml exec -T personal-https python cloud_admin.py invite desktop --url https://服务器公网IP --certificate /run/lanxi-tls/server.crt
```

将命令输出的整行配对码粘贴到首台设备。公开受信证书的域名部署可使用 `coordinator` 服务执行同一命令，省略 `--certificate`。服务器的证书更换后，用新证书生成配对码重新配对。

## 无域名的私人部署

以下是支持旧版客户端的手工 SSH 隧道配对流程。

在 Linux 服务器的 `deploy/coordinator` 目录使用 `compose.ssh.yaml`。它在服务器回环地址 `127.0.0.1:8770` 运行后端。使用已有的 SSH 入站规则即可，协调接口保留在服务器内部。

```bash
sudo docker compose -f compose.ssh.yaml build
sudo docker compose -f compose.ssh.yaml run --rm --no-deps coordinator cloud_admin.py init desktop laptop
sudo docker compose -f compose.ssh.yaml up -d --wait
curl -fsS http://127.0.0.1:8770/healthz
```

每台 Windows 电脑用自己的 SSH 密钥登录，并从服务器控制台核对主机指纹。隧道把这台电脑的 `127.0.0.1:18770` 转发到服务器的 `127.0.0.1:8770`。把命令里的账号和地址换成自己的。保留运行隧道的窗口，断线后重新执行即可。 [OpenSSH 端口转发](https://man.openbsd.org/ssh)

```powershell
ssh -N -T -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -L 127.0.0.1:18770:127.0.0.1:8770 admin@服务器公网地址
```

在另一个 PowerShell 窗口检查 `Invoke-RestMethod http://127.0.0.1:18770/healthz` 是否返回 `{"status":"ready"}`。两台电脑各自运行同样的检查。服务器管理员分别生成设备配置，地址都是各电脑自己隧道的回环地址。

```bash
sudo docker compose -f compose.ssh.yaml exec coordinator python cloud_admin.py pair desktop --url http://127.0.0.1:18770
sudo docker compose -f compose.ssh.yaml exec coordinator python cloud_admin.py pair laptop --url http://127.0.0.1:18770
```

按下文的 Windows 配对步骤合并配置并设置各自的令牌。SSH 负责加密跨机传输。服务器的持久卷名称和 HTTPS 部署一致，今后切换到域名时继续用同一份数据，直接重建服务即可。

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

从服务器分别生成两台电脑的配置。使用域名时运行下面的命令，SSH 隧道使用上面的命令。输出包含客户端 JSON 和该设备专用的 PowerShell 令牌设置。

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

个人版私有 HTTPS 容器从 `/run/lanxi-releases/latest.json` 读取已发布版本。管理员先上传完整 ZIP 并确认 SHA-256，再替换含 `version`、`sha256` 和 `size` 的清单。客户端使用原 HTTPS 配对凭据读取更新。使用 Linux 文件名和换行符。

R28 按需借用文件使用持久卷下的 `temporary-files/`。服务器为系统保留至少 5 GiB，并以 SHA-256 校验完整文件。借用成功或检测到来源冲突后保留交接记录，清除临时内容。

设备登记和 SQLite 位于 `coordinator-data` 持久卷。容器重启和重建会继续使用原数据。Caddy 的证书数据保存在自己的持久卷中。使用 SSH 部署时在以下 Compose 命令中加上 `-f compose.ssh.yaml`，日志只查看 `coordinator` 服务。

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
