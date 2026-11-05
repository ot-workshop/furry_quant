# Linux 运维

生产环境由 Docker Compose 运行 PostgreSQL、FastAPI 和一个串行 worker。应用镜像由 GitHub Actions 构建并保存在私有 GHCR；服务器不编译源码，也不接受 GitHub 的自动部署连接。

## 目录与配置

部署目录只需要：

```text
finance-quant/
├── docker-compose.yaml
├── .env
├── scripts/
│   ├── backup.sh
│   └── restore.sh
├── storage/          # 可以指向目录外的绝对路径
└── backups/          # 可以指向目录外的绝对路径
```

`.env` 至少配置：

```dotenv
APP_IMAGE=ghcr.io/quqionfree/my_quant_project:sha-实际提交号
TUSHARE_TOKEN=官方TushareToken
POSTGRES_DB=finance_quant
POSTGRES_USER=quant
POSTGRES_PASSWORD=独立强密码
STORAGE_ROOT=/home/用户名/finance-quant-data/storage
BACKUP_ROOT=/home/用户名/finance-quant-data/backups
HTTP_PORT=8000
```

`.env` 权限设为 `600`。GHCR Token 不写入 `.env`，只用于 `docker login`。

## 私有镜像认证

创建 classic personal access token，只授予 `read:packages`。对应 GitHub 账户需要拥有私有仓库的读取权限。

```sh
read -s GHCR_TOKEN
echo "$GHCR_TOKEN" | docker login ghcr.io -u GitHub用户名 --password-stdin
unset GHCR_TOKEN
```

登录状态保存在部署用户的 Docker 配置中。不要用 root 登录后再以普通用户运行 Compose，否则两者使用不同的凭据文件。

## 首次启动

校验插值后的配置：

```sh
docker compose --env-file .env -f docker-compose.yaml config
```

拉取 PostgreSQL 和应用镜像：

```sh
docker compose --env-file .env -f docker-compose.yaml pull
```

启动数据库并等待健康：

```sh
docker compose --env-file .env -f docker-compose.yaml up -d db
docker compose --env-file .env -f docker-compose.yaml ps
```

显式执行数据库迁移，再启动 API 和 worker：

```sh
docker compose --env-file .env -f docker-compose.yaml run --rm migrate
docker compose --env-file .env -f docker-compose.yaml up -d api worker
```

检查服务：

```sh
docker compose --env-file .env -f docker-compose.yaml ps
docker compose --env-file .env -f docker-compose.yaml logs --tail=100 api worker
docker compose --env-file .env -f docker-compose.yaml exec -T api python -m finance_quant.server check
wget -qO- http://127.0.0.1:8000/api/v1/health
```

`db` 和 `api` 应为 healthy，`worker` 应保持运行。`migrate` 是一次性命令，不是常驻服务。

## 更新镜像

从 GitHub Actions 复制新的 `sha-*` 标签或镜像 digest，修改 `.env` 的 `APP_IMAGE`。确认没有排队或运行中的任务后执行：

```sh
docker compose --env-file .env -f docker-compose.yaml pull
docker compose --env-file .env -f docker-compose.yaml stop -t 90 worker api
docker compose --env-file .env -f docker-compose.yaml run --rm migrate
docker compose --env-file .env -f docker-compose.yaml up -d api worker
docker compose --env-file .env -f docker-compose.yaml ps
docker compose --env-file .env -f docker-compose.yaml logs --tail=100 api worker
```

数据库在应用更新期间保持运行。worker 收到停止信号后停止领取新任务，并给当前子进程保留宽限时间。

## 更新 Compose

普通应用版本只需要更换 `APP_IMAGE`。当服务、volume、端口或环境变量结构发生变化时，再从本地仓库复制新版文件：

```powershell
scp docker-compose.yaml 用户名@服务器:~/finance-quant/docker-compose.yaml.new
```

服务器先校验新文件，再替换：

```sh
docker compose --env-file .env -f docker-compose.yaml.new config
mv docker-compose.yaml.new docker-compose.yaml
```

私有仓库不提供匿名 raw 下载。若必须从 GitHub API 下载，需要单独的仓库 Contents 只读凭据；该凭据与 GHCR 的 `read:packages` Token 权限不同。

## 回滚

将 `APP_IMAGE` 改回之前验证过的 tag 或 digest：

```sh
docker compose --env-file .env -f docker-compose.yaml pull
docker compose --env-file .env -f docker-compose.yaml stop -t 90 worker api
docker compose --env-file .env -f docker-compose.yaml up -d api worker
```

镜像回滚不会自动回滚数据库。迁移应采用向前兼容的扩展/收缩方式；删除列、修改字段类型等破坏性操作拆分到后续版本。无法兼容时恢复部署前备份。

## 日志与状态

```sh
docker compose --env-file .env -f docker-compose.yaml ps
docker compose --env-file .env -f docker-compose.yaml logs -f api worker
docker compose --env-file .env -f docker-compose.yaml logs --tail=100 db
```

任务子进程日志位于 `STORAGE_ROOT/logs/`。网页关闭或 SSH 隧道断开不会停止服务器任务。

只重启应用：

```sh
docker compose --env-file .env -f docker-compose.yaml restart api worker
```

不要执行 `docker compose down -v`。`-v` 会删除 PostgreSQL volume。

## Windows 访问

API 只发布到服务器回环地址。Windows PowerShell 建立隧道：

```powershell
ssh -N -L 8000:127.0.0.1:8000 用户名@服务器地址
```

浏览器打开 <http://127.0.0.1:8000>。隧道只影响浏览器访问，不影响后台任务。

## 备份

没有排队或运行中的任务时执行：

```sh
sh scripts/backup.sh
```

备份包括：

- PostgreSQL 自定义格式 dump。
- 研究运行实际引用的不可变输入数据集。
- 持仓、因子明细、标签和报告文件。
- artifact 清单、schema 版本和 SHA-256 校验文件。

缓存、原始供应商响应和 `.env` 不进入备份。Docker volume 是持久化手段，不是独立备份。

## 恢复

恢复目标必须是空项目数据库，并且 storage 中不存在清单内的同名文件：

```sh
sh scripts/restore.sh /path/to/backups/20261105T000000Z
```

恢复脚本校验哈希和相对路径，停止 API/worker，恢复数据库与登记文件，再重新启动应用。建议先用另一个 `COMPOSE_PROJECT_NAME`、storage 路径和 HTTP 端口做恢复演练。

## 磁盘与清理

定期检查：

```sh
df -h
docker system df
du -sh storage backups
```

不得手动删除 `datasets/` 或 `runs/` 中仍被 PostgreSQL 登记的文件。Docker 构建缓存可清理，但数据库 volume、正式镜像和备份必须先确认引用关系。
