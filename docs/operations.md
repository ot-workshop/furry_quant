# Linux 运维

生产环境由 Docker Compose 运行 PostgreSQL、FastAPI 和一个串行 worker。应用镜像由 GitHub Actions 构建并保存在私有 GHCR；服务器不编译源码，也不接受 GitHub 的自动部署连接。

## 目录与配置

部署目录只需要：

```text
finance-quant/
├── docker-compose.yaml
├── .env
├── storage/          # 可以指向目录外的绝对路径
├── backups/          # 可以指向目录外的绝对路径
└── restores/         # 网页恢复使用的独立目录
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
RESTORE_ROOT=/home/用户名/finance-quant-data/restores
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

网页“数据管理 → 备份与恢复”提供创建、校验和恢复入口，风格与其他数据页面一致。后台任务与行情更新共用串行 worker，备份执行期间阻止 API 写入；无需在网页操作时停掉 worker。下列命令行方式则需要先停止 API 与 worker。

停止 API 与 worker 后执行；数据库保持运行。备份命令会检查 worker 互斥锁并阻止 API 写入，不会仅凭“当前没有运行中的任务”判断一致性：

```sh
docker compose stop -t 90 api worker
docker compose run --rm maintenance python -m finance_quant.backup create /backups/backup-20261112
docker compose run --rm maintenance python -m finance_quant.backup verify /backups/backup-20261112
docker compose up -d api worker
```

备份包括：

- PostgreSQL 自定义格式 dump。
- 整个 STORAGE_ROOT：共享行情、原始响应、财务观测与不可变版本、历史行业、基准、下载检查点、冻结数据集、研究结果、日志与配额记录。
- 每个文件及归档的 SHA-256 清单；运行中的锁文件不备份。

`.env` 不进入备份，凭据应另外保管。所有数据位置必须位于 STORAGE_ROOT 内；遇到目录外的覆盖配置时命令会说明情况并停止，不会擅自移动已有数据。备份目标必须在 STORAGE_ROOT 之外，且不存在。完整归档需要足够磁盘空间。Docker volume 是持久化手段，不是独立备份。

原生开发环境也使用相同命令：

```powershell
uv run python -m finance_quant.backup create D:/quant-backups/backup-20261112 --env-file .env.development
uv run python -m finance_quant.backup verify D:/quant-backups/backup-20261112
```

需安装与服务器主版本相同或更高的 PostgreSQL 客户端，并将其 bin 加入 PATH；也可设置 `POSTGRES_BIN`，例如 `D:/Program Files/PostgreSQL/18/bin`。应用容器已经包含 PostgreSQL 18 的备份客户端。

## 恢复

网页恢复填写新环境名称，数据库强制使用 `fq_restore_` 前缀，文件存入 `RESTORE_ROOT/<数据库名>`；确认窗口展示目标位置。后台先校验归档，再创建不存在的目标库，仅在空库和空目录上恢复。已有数据的目标会被拒绝，当前应用连接不会切换。

若当前数据库账号没有 CREATEDB 权限，可由管理员预建同名空库，或在环境配置 `RESTORE_POSTGRES_USER` 与 `RESTORE_POSTGRES_PASSWORD`，然后重启应用。凭据只在服务器使用，不发送到网页。恢复路径、备份路径需由部署用户提前建立并赋予应用容器用户写权限。

为恢复环境单独创建目录，复制 Compose 与 `.env`，修改以下配置。不要使用现有部署目录：

```dotenv
COMPOSE_PROJECT_NAME=finance-quant-restore-check
STORAGE_ROOT=/home/用户名/finance-quant-restore-check/storage
BACKUP_ROOT=/home/用户名/finance-quant-data/backups
HTTP_PORT=8001
```

不同 Compose 项目名会创建独立数据库 volume。恢复目标必须是全新空数据库和空存储目录；不要先运行 migrate。恢复命令会在任何数据库写入前检查实际数据库身份、空库、空目录及完整归档校验：

```sh
docker compose up -d db
docker compose run --rm maintenance python -m finance_quant.backup restore /backups/backup-20261112
docker compose run --rm maintenance python -m finance_quant.server check
docker compose up -d api worker
```

恢复不会执行清库或覆盖已有文件。原排队、运行中任务改为中断状态，自动更新计划关闭，确认数据后再手动续传或开启计划。已完成任务、数据版本及结果保留。恢复失败时保留临时目录便于排查，不会清理原环境。自定义目录布局可在备份 manifest 的 layout 中查看，并在恢复环境配置相同的相对布局。

## 统一数据更新

网页“数据管理 → 统一更新”可设置股票范围、起止日期、行情预热和修订回看长度，并选择财务、申万行业级别和基准。也可关闭行情更新，仅选择已发布行情版本更新对应股票的财务和基准。

任务按“行情与参考数据 → 财务 → 行业 → 基准”串行执行。行情未完整发布时，对应财务和基准步骤显示等待依赖，独立行业步骤仍可执行。各类版本独立发布，报告逐项状态与版本，不把部分成功称为整套成功。续传复用已完成的步骤和报表/行业分区缓存，旧版本不删除。

自动计划使用北京时间，每天到设定小时后入队一次；worker 每分钟检查。午夜至17点的计划使用前一日作为数据截止日，18点后的计划使用当日。worker 不在线时不执行，恢复在线后补入当日到期任务；不自动堆积历史日期的任务。多个数据源仍使用原有配额控制，BaoStock保持串行。

## 隔离测试

完整备份恢复演练使用独立测试服务，无生产 volume、主机目录挂载或数据库端口暴露：

```sh
docker compose -f docker-compose.test.yaml up --build --abort-on-container-exit --exit-code-from test
docker compose -f docker-compose.test.yaml down --volumes
```

第二条命令只清理命名为 finance-quant-isolated-tests 的测试服务。原生测试必须显式指定测试数据库和临时存储，数据库名为 fq_* 或 *_test；测试夹具拒绝开发库配置。备份恢复测试在测试服务器中另建一次性数据库，并在任何写入前核验实际连接。

## 磁盘与清理

定期检查：

```sh
df -h
docker system df
du -sh storage backups
```

不得手动删除 `datasets/` 或 `runs/` 中仍被 PostgreSQL 登记的文件。Docker 构建缓存可清理，但数据库 volume、正式镜像和备份必须先确认引用关系。
