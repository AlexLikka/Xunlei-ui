# Linux / Docker 平台部署

Thunder 支持公开注册与独立用户空间。新用户使用用户名和至少 12 位密码注册，注册后自动登录，初始机器列表为空。平台不使用初始化码。每个账号独立保存机器、SSH 凭据、主机密钥、任务操作记录、自动清理设置和速度历史。

## 启动与访问

安装 Docker Engine 和 Compose 插件后，在项目目录执行：

```sh
sudo docker compose up -d --build --wait
```

默认监听所有 IPv4 网卡的 `8765` 端口。在浏览器打开 `http://服务器IP:8765`，选择登录或注册账号。服务器的网络防火墙或云安全组也需允许 TCP 8765。

登录后点击「添加机器」，填写机器 IP 和 SSH 端口，输入 SSH 账号密码，核对主机密钥指纹后添加。复用凭据仅限当前账号已有的机器。新机器默认关闭自动清理；机器设置及任务操作均只作用于当前用户空间。

平台服务器需要能连接目标 Windows 的 SSH，且目标机器的迅雷正在运行。普通监控不需要在远程 Windows 安装 Python；云盘功能会上传 PowerShell 模块并登记一次性计划任务，要求交互用户保持登录且迅雷已登录云盘。访问私网目标时需要已有的 VPN 或内网路由。

```sh
sudo docker compose ps
sudo docker compose logs --tail=100 thunder
sudo docker compose stop                # 管理员停止整个平台
sudo docker compose up -d --wait         # 恢复运行
sudo docker compose up -d --build --wait # 更新代码后重建
```

Docker 服务应启用开机自启。容器的 `restart: unless-stopped` 会在进程退出及宿主机重启后自动恢复；手动停止后需要再次执行启动命令。容器每 30 秒检查 HTTP 服务。退出登录不会停止后台监控；网页账号不能关闭整个平台。修改用户名或密码会注销此账号的全部会话，不影响其他账号，机器仍归属于原来的固定用户 ID。

## 持久化与备份

命名卷 `thunder-tool_thunder-data` 挂载到 `/data`，容器使用 UID 10001 运行。当前数据布局：

- `/data/.runtime/users.sqlite3`：用户 ID、用户名、盐和 PBKDF2 密码哈希。
- `/data/users/<用户ID>/config.json`：此用户的机器与监控设置。
- `/data/users/<用户ID>/.runtime/credentials/`：此用户在网页保存的 SSH 凭据。
- `/data/users/<用户ID>/.runtime/known_hosts`：此用户确认的 SSH 主机密钥。
- `/data/users/<用户ID>/.runtime/events.jsonl`：此用户的操作及禁止重复执行记录。
- `/data/.runtime/legacy-owner.json`：旧数据迁移归属记录。

迁移保留的旧凭据文件可能位于用户目录下的其他相对路径。数据目录及凭据文件限制其他系统用户访问；密码、账号数据库和配置不会被打包进镜像。网页登录会话在进程重启后失效，所有机器配置和账号保留。

备份前停止服务，确保数据库与操作记录一致：

```sh
sudo docker compose stop
mkdir -p backup
chmod 700 backup
sudo docker compose cp thunder:/data/. ./backup/
sudo chmod -R go-rwx backup
sudo docker compose up -d --wait
```

备份包含 SSH 凭据，应保存在受限或加密位置。不要执行 `docker compose down -v`，它会删除平台数据。恢复时保留完整目录结构和用户数据库，并将所有者设置为 UID/GID 10001。

## 从旧版单用户数据迁移

旧机器不会自动分配给第一个注册者。先备份并停止旧服务，构建新镜像，再使用离线迁移工具将旧数据归到明确指定的账号；账号不存在时会创建它。以下命令从终端隐藏读取密码，密码不写进镜像或 Compose 配置：

```sh
read -rs -p '目标账号密码：' THUNDER_MIGRATION_PASSWORD
printf '%s\n' "$THUNDER_MIGRATION_PASSWORD" | sudo docker compose run --rm --no-deps -T \
  --entrypoint python thunder migrate_users.py --username 目标用户名 --password-stdin
unset THUNDER_MIGRATION_PASSWORD
sudo docker compose up -d --wait
```

迁移保留旧机器配置、SSH 凭据、主机密钥、自动清理开关和操作记录。相对凭据路径保留，绝对路径需先改成数据目录内的相对路径。重复导入到同一账号会检查迁移记录；拒绝覆盖其他账号已有的机器。新注册用户始终从空工作空间开始。

## HTTPS 与监听设置

可通过 `.env` 调整监听网卡，例如只允许反向代理访问：

```dotenv
THUNDER_PUBLISH_IP=127.0.0.1
THUNDER_SECURE_COOKIE=1
```

`THUNDER_SECURE_COOKIE=1` 适用于 HTTPS 入口，此时浏览器只通过 HTTPS 发送会话 Cookie。反向代理需保留原始 Host。直接使用 `http://服务器IP:8765` 时保持默认的 `THUNDER_SECURE_COOKIE=0`。

## 低配服务器与封档停机

Compose 默认为 2 核 / 2 GB 主机限制容器资源：384 MB 内存、1 核 CPU、128 个进程 / 线程。可在 `.env` 设置 `THUNDER_MEMORY_LIMIT`（例如 `512m`）和 `THUNDER_CPU_LIMIT`，重建容器后生效。

如果构建环境必须经过代理，可使用 Docker 标准构建代理参数；代理地址取决于自己的网络环境，不需要写入镜像或提交仓库。

```sh
sudo docker build --build-arg HTTP_PROXY --build-arg HTTPS_PROXY -t thunder-tool-thunder .
sudo docker compose up -d --no-build --wait
```

最终源码于 2026-10-29 封档。长期停止并关闭现有容器的自动重启：

```sh
sudo docker update --restart=no thunder-tool-thunder-1
sudo docker compose stop
sudo docker inspect thunder-tool-thunder-1 --format '{{.State.Status}} {{.HostConfig.RestartPolicy.Name}}'
```

这些命令保留容器与数据卷。以后执行 `docker compose up -d` 会主动重新启动服务，也可能重新应用 Compose 中的 `unless-stopped` 策略。停止 Web 不会取消已经提交到 Windows 的一次性计划任务或迅雷下载。

仓库仅含代码和空配置示例；真实账号、机器、凭据、备份及原始含凭据 CLI 均不发布。已验证的迅雷版本为 Windows `25.1.12.1624`；其他版本情况及功能限制见 [README](../README.md)。
