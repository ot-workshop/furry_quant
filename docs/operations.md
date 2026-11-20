# Linux 运维

生产环境由 Docker Compose 运行 PostgreSQL、FastAPI 和一个串行 worker。应用镜像由 GitHub Actions 构建并保存在私有 GHCR；服务器不编译源码，也不接受 GitHub 的自动部署连接。

## 目录与配置

部署目录只需要：

```text
finance-quant/
├── docker-compose.yaml
├── .env
├── postgres/         # PostgreSQL 数据目录，由官方镜像管理
├── storage/          # 自动创建；支持相对路径或绝对路径
├── backups/          # 自动创建；支持相对路径或绝对路径
└── restores/         # 自动创建；独立恢复环境
```

`.env` 至少配置：

```dotenv
APP_IMAGE=ghcr.io/quqionfree/my_quant_project:sha-实际提交号
TUSHARE_TOKEN=官方TushareToken
POSTGRES_DB=finance_quant
POSTGRES_USER=quant
POSTGRES_PASSWORD='独立强密码'
POSTGRES_DATA_ROOT=./postgres
STORAGE_ROOT=./storage
BACKUP_ROOT=./backups
RESTORE_ROOT=./restores
HTTP_PORT=8000
```

`.env` 权限设为 `600`。GHCR Token 不写入 `.env`，只用于 `docker login`。

PostgreSQL 使用 `POSTGRES_DATA_ROOT` 主机目录，默认 `./postgres`，目录权限由官方镜像管理。其余三个目录继续使用 `./storage`、`./backups`、`./restores`。Compose 默认以部署文件夹名作为项目名；同一主机运行多套实例时，使用不同文件夹名、端口和数据库目录。文件夹同名或需要自定义时，可设置 `COMPOSE_PROJECT_NAME`；不同项目名不会隔离指向同一路径的挂载。

## 私有镜像认证

创建 classic personal access token，只授予 `read:packages`。对应 GitHub 账户需要拥有私有仓库的读取权限。

```sh
read -s GHCR_TOKEN
echo "$GHCR_TOKEN" | docker login ghcr.io -u GitHub用户名 --password-stdin
unset GHCR_TOKEN
```

登录状态保存在部署用户的 Docker 配置中。不要用 root 登录后再以普通用户运行 Compose，否则两者使用不同的凭据文件。

## 首次启动

在部署目录检查配置并启动服务：

```sh
docker compose config --quiet
docker compose pull
docker compose up -d
docker compose ps -a
```

首次启动由 PostgreSQL 官方镜像创建用户和数据库；账号、库名和密码来自 `.env`。数据库健康检查使用与应用相同的 `db` 网络地址进行密码认证并执行查询，只有最终数据库进程可以通过检查。不能使用 `127.0.0.1` 代替：官方镜像的回环认证规则可能为 `trust`，这会跳过密码校验。

`storage-init` 准备三个主机挂载根目录的属主，不递归修改业务文件。`migrate` 等待数据库健康与目录准备成功后执行迁移；API、worker 在迁移成功后启动。API、worker 和迁移均以非 root 用户运行。

正常状态为：`db`、`api` 健康，`worker` 运行，`storage-init`、`migrate` 以状态码0退出。一次性服务正常退出后无需保持运行。

验证应用及查看启动日志：

```sh
docker compose exec api python -m finance_quant.server check
docker compose logs --tail=100 db storage-init migrate api worker
```

准备或迁移失败会阻止依赖它们的应用服务启动。处理失败原因后，重新执行 `docker compose up -d`。重复启动会复用现有数据库和主机目录，迁移只应用尚未执行的版本。

## 更新镜像

从 GitHub Actions 获取目标镜像标签或 digest，更新 `.env` 中的 `APP_IMAGE`。确认没有运行中的任务后执行：

```sh
docker compose pull
docker compose stop -t 90 worker api
docker compose up -d
docker compose ps -a
```

数据库保持运行。worker 收到停止信号后停止领取新任务，并给当前子进程保留退出时间。新镜像的迁移自动执行，数据库卷与主机文件继续保留。

### 更新后继续未完成下载

保留 `.env` 中的数据库配置和 `POSTGRES_DATA_ROOT`、`STORAGE_ROOT` 路径；不要清理数据库、`storage`、原始响应或下载计划。更新应用只需改 `APP_IMAGE`，使用 GitHub Actions 已成功发布且包含目标修复的标签或 digest。仅修改本地代码、重启旧镜像或拉取同一个固定旧标签不会获得修复。

容器健康后，打开“数据管理 → 统一更新”，在原任务的“统一更新记录”中点击“续传”。单独创建的行情任务在“行情下载 → 下载记录”中续传。程序复用原计划、完成断点和接口缓存，保留成功的数据与研究结果；校验规则更新后，未完成候选会用新规则重新核验，必要时补查缺失历史。

续传后展开记录，检查剩余待处理股票及自动处理明细。存在来源冲突或缺少历史证据时，任务仍可能显示部分完成；不要通过删除股票或清空数据来绕过复核。

## 更新 Compose

普通应用版本只需要更换 `APP_IMAGE`。当服务、volume、端口或环境变量结构发生变化时，再从本地仓库复制新版文件：

```powershell
scp docker-compose.yaml 用户名@服务器:~/finance-quant/docker-compose.yaml.new
```

服务器先校验新文件，再替换：

```sh
docker compose --env-file .env -f docker-compose.yaml.new config --quiet
mv docker-compose.yaml.new docker-compose.yaml
```

私有仓库不提供匿名 raw 下载。若必须从 GitHub API 下载，需要单独的仓库 Contents 只读凭据；该凭据与 GHCR 的 `read:packages` Token 权限不同。

## 回滚

将 `APP_IMAGE` 改回之前验证过的 tag 或 digest：

```sh
docker compose --env-file .env -f docker-compose.yaml pull
docker compose --env-file .env -f docker-compose.yaml stop -t 90 worker api
docker compose --env-file .env -f docker-compose.yaml up -d
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

`docker compose down` 停止并删除本项目容器和网络，主机上的数据库、业务、备份和恢复目录继续保留。重建试运行数据库见下文。

## Windows 访问

API 只发布到服务器回环地址。Windows PowerShell 建立隧道：

```powershell
ssh -N -L 8000:127.0.0.1:8000 用户名@服务器地址
```

浏览器打开 <http://127.0.0.1:8000>。隧道只影响浏览器访问，不影响后台任务。

直接通过局域网访问时，将 `HTTP_BIND_HOST` 设置为服务器地址，并在 `ALLOWED_ORIGINS` 中加入完整网页地址。例如 `HTTP_BIND_HOST=0.0.0.0`（监听所有网卡）、`ALLOWED_ORIGINS=http://10.88.0.61:8000`，执行 `docker compose up -d` 后即可从其他电脑访问。监听地址决定端口是否对外可达，来源配置决定网页能否提交研究和数据管理操作；两项应配套设置。

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

若当前数据库账号没有 CREATEDB 权限，可由管理员预建同名空库，或在环境配置 `RESTORE_POSTGRES_USER` 与 `RESTORE_POSTGRES_PASSWORD`，然后重启应用。凭据只在服务器使用，不发送到网页。使用 Compose 时，备份与恢复目录由 storage-init 自动准备写权限。

为恢复环境单独创建目录，复制 Compose 与 `.env`，修改以下配置。不要使用现有部署目录：

```dotenv
COMPOSE_PROJECT_NAME=finance-quant-restore-check
POSTGRES_DATA_ROOT=./postgres
STORAGE_ROOT=/home/用户名/finance-quant-restore-check/storage
BACKUP_ROOT=/home/用户名/finance-quant/backups
HTTP_PORT=8001
```

恢复环境使用独立的部署目录、项目名、数据库目录和业务目录；备份目录可以指向原归档位置。恢复目标必须是全新空数据库和空存储目录；不要先运行 migrate。CLI 恢复的目标是**可写宿主父目录下的独立子目录**，不要把 `/app/storage` 这类挂载点本身作为恢复目标，也不要提高容器用户权限。先在恢复项目的 `.env` 中给 `RESTORE_ROOT` 指定专用宿主父目录（例如宿主上的 `/home/用户名/finance-quant-restore-check/restores`，容器内为 `/restores`），目标子目录（例如 `recovered`）可以尚不存在：

```sh
docker compose up -d db
docker compose --profile tools run --rm -e STORAGE_ROOT=/restores/recovered \
  maintenance python -m finance_quant.backup restore /backups/backup-20261112
```

恢复进程按固定顺序执行：先校验归档完整性，再对恢复目标做只读检查（父目录可写、目标不是挂载点、不是符号链接、不存在或为空），这些检查失败时不会打开数据库、不会删除或创建任何目录；检查通过后才建立离线锁、确认实际数据库身份和目标库为空，随后才移除已存在的空目标并由当前用户重建、创建私有暂存目录，最后解包文件并执行 `pg_restore`。挂载点、父目录不可写、非空目标、无法移除的已有空目录或数据库非空都会在写库前拒绝，已有空目录无法移除时保持原样。不要用 `--user 0:0` 恢复，否则文件归 root，后续服务用户无法写入。

恢复成功后保留同一项目和数据库，把项目 `.env` 的 `STORAGE_ROOT` 改为宿主的 `<恢复父目录>/recovered`，使该目录挂载为 `/app/storage`，再启动服务并检查同一运行：

```sh
docker compose --profile tools run --rm maintenance python -m finance_quant.server check
docker compose up -d api worker
```

`server check` 只检查表结构和服务可用；恢复是否完整还要从 API 读回同一 `run_id` 和 `dataset_id`，并核对冻结输入与结果文件。

恢复不会执行清库或覆盖已有文件。原排队、运行中任务改为中断状态，自动更新计划关闭，确认数据后再手动续传或开启计划。已完成任务、数据版本及结果保留。发布前检查失败不会写入数据库或目标目录；已有空目标无法移除时保持原样，不同级其他挂载不会阻断合法子目录。不会清理原环境。自定义目录布局可在备份 manifest 的 layout 中查看，并在恢复环境配置相同的相对布局。

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

## 试运行环境重建

官方镜像的初始化参数只对空数据库目录生效。修改 `.env` 中的密码不会改变已有数据库密码，修改库名也不会迁移原库内容。已有业务数据的环境应保留原凭据，或执行明确的账号变更与数据迁移。

确认本项目试运行数据库可丢弃时，可在对应部署目录执行：

```sh
docker compose down
mv postgres "postgres.previous-$(date +%Y%m%d-%H%M%S)"
docker compose up -d
```

以上示例使用默认 `./postgres` 路径：先停止服务，将旧数据库目录移开，再按当前 `.env` 初始化新的空目录。自定义 `POSTGRES_DATA_ROOT` 时，应操作实际配置的目录。storage、backups 和 restores 保留；保留的业务文件不会自动注册为新实例的研究结果。

旧版曾使用的 `finance-quant_postgres` 命名卷不会自动转为主机目录。需要保留旧库时应先备份，再恢复至独立环境；可丢弃的试运行库可直接切换到新的空目录，确认启动成功后再清理旧命名卷。

## 启动流程验证

在 Linux 上使用本地应用镜像验证首次启动、主机目录写权限、重复启动、升级迁移、迁移失败阻断以及旧卷密码不匹配：

```sh
docker build --target application -t finance-quant:startup-check .
python3 scripts/check_compose_startup.py --image finance-quant:startup-check
```

每个场景使用随机 Compose 项目，只挂载脚本创建的临时数据库和业务目录，结束后清理测试资源。GitHub Actions 在发布应用镜像前执行同一组验证。
