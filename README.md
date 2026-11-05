# Finance Quant Research Workbench

面向 A 股日线研究的因子分析与回测工作台。项目使用官方 Tushare 管理行情和参考数据，以 PostgreSQL 保存数据版本、任务及结果，由独立 worker 调用 Qlib 完成因子计算、组合回测和因子评估，并通过 React 网页展示研究配置与结果。

项目用于研究，不连接券商交易接口，也不承诺历史收益能够在实盘复现。

## 功能

- 管理自选股票、沪深300历史成分股和共享全市场行情。
- 按研究区间自动计算预热范围，支持增量更新和不可变数据集快照。
- 提供动量、反转、波动、估值、流动性、回撤和市值等可配置因子。
- 支持历史行业诊断与可选的因子行业中性化。
- 支持日、周、月调仓，以及佣金、最低佣金、印花税、过户费和冲击成本。
- 模拟停牌、涨跌停、整手、板块交易规则和动态成分股退出。
- 支持价格指数与全收益指数基准。
- 展示净值、回撤、风险指标、成交、持仓、费用、行业暴露和 IC/IR。
- 为每次研究保存配置、数据版本、输入哈希和结果文件关系。
- 支持 PostgreSQL 与研究输入/结果文件的一致性备份和恢复。

## 技术栈

- Python 3.12、FastAPI、Pandas、PyArrow、Qlib
- PostgreSQL 18、SQLAlchemy、Alembic
- React 19、TypeScript、Vite、Ant Design、ECharts
- Docker Compose（仅生产运行）
- GitHub Actions、GitHub Container Registry（私有镜像构建）

## 架构

```text
Browser
   │
   ▼
FastAPI ─────────────── PostgreSQL
   │                         │
   └── submit/query          ├── market facts and releases
                             ├── task queue
Worker ── Qlib subprocess    └── result metadata
   │
   └── storage/
       ├── datasets/   immutable research inputs
       ├── runs/       parquet details and reports
       ├── raw/        provider responses
       └── logs/       task process logs
```

API 只提交和查询任务。单 worker 从 PostgreSQL 队列领取任务，并用 advisory lock 保证同一部署只有一个有效消费者。Qlib 读取冻结后的研究输入，不在逐日回测循环中查询正在变化的市场数据库。

详细设计见 [系统架构](docs/architecture.md)。

## 本地开发

本地开发不使用 Docker。需要安装：

- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- Node.js 22
- PostgreSQL 18

### 1. 安装依赖

```powershell
uv sync --locked --extra dev
npm.cmd --prefix frontend ci --no-audit --no-fund
```

### 2. 创建开发数据库

使用 PostgreSQL 管理账户执行：

```sql
CREATE ROLE quant_dev WITH LOGIN PASSWORD '请设置独立开发密码';
CREATE DATABASE finance_quant_dev OWNER quant_dev;
```

复制开发环境模板：

```powershell
Copy-Item .env.development.example .env.development
```

填写 `POSTGRES_PASSWORD` 和 `TUSHARE_TOKEN`。开发数据库、`storage-dev/` 和生产环境必须相互独立。

执行迁移：

```powershell
$env:FINANCE_QUANT_ENV_FILE = ".env.development"
uv run alembic upgrade head
Remove-Item Env:FINANCE_QUANT_ENV_FILE
```

### 3. 启动开发服务

后端 API：

```powershell
uv run python -m finance_quant.server api --env-file .env.development --reload --host 127.0.0.1 --port 8000
```

任务 worker：

```powershell
uv run python -m finance_quant.server worker --env-file .env.development
```

前端开发服务器：

```powershell
npm.cmd --prefix frontend run dev
```

打开 <http://127.0.0.1:5173>。Vite 将 `/api` 请求代理到 `127.0.0.1:8000`。

### 4. 本地检查

```powershell
uv run pytest -q
npm.cmd --prefix frontend test
npm.cmd --prefix frontend run build
```

真实 Tushare 下载不属于默认单元测试。进行真实数据验证时使用单独的开发数据库和短日期、小股票池配置。

## 私有镜像构建

`.github/workflows/docker.yml` 在以下情况构建 `linux/amd64` 镜像并推送到私有 GHCR：

- 推送到 `main`
- 推送 `v*` 标签
- 在 GitHub Actions 手动触发

镜像名称：

```text
ghcr.io/quqionfree/my_quant_project
```

镜像标签包括不可变提交标签 `sha-<commit>`、Git 标签和手动构建编号。正式部署应使用 `sha-*` 标签或工作流输出的镜像 digest，不依赖浮动的 `main` 标签。

工作流只使用 GitHub 自动生成的 `GITHUB_TOKEN` 写入 GHCR，不保存服务器地址、SSH 密钥、Tushare Token 或数据库密码。完整测试可通过 `.github/workflows/full-tests.yml` 在 Pull Request 或手动触发时运行。

## Linux 部署

服务器只需要 Docker Engine、Docker Compose v2、`docker-compose.yaml` 和 `.env`，不需要 Git、Node.js 或 Python 开发环境。

### 1. 准备部署目录

从本地仓库复制不含凭据的 Compose 和环境模板：

```powershell
scp docker-compose.yaml 用户名@服务器:~/finance-quant/
scp .env.example 用户名@服务器:~/finance-quant/
```

服务器上执行：

```sh
cd ~/finance-quant
cp .env.example .env
chmod 600 .env
mkdir -p storage backups
```

在 `.env` 中填写：

```dotenv
APP_IMAGE=ghcr.io/quqionfree/my_quant_project:sha-实际提交号
TUSHARE_TOKEN=官方TushareToken
POSTGRES_PASSWORD=独立强密码
STORAGE_ROOT=/home/用户名/finance-quant-data/storage
BACKUP_ROOT=/home/用户名/finance-quant-data/backups
```

### 2. 登录私有 GHCR

GitHub Container Registry 当前要求服务器使用 classic personal access token 拉取私有镜像。Token 只授予 `read:packages`，对应账户需要拥有仓库读取权限。

```sh
read -s GHCR_TOKEN
echo "$GHCR_TOKEN" | docker login ghcr.io -u GitHub用户名 --password-stdin
unset GHCR_TOKEN
```

不要把 GHCR Token 写入项目 `.env`。

### 3. 首次启动

```sh
docker compose --env-file .env -f docker-compose.yaml config
docker compose --env-file .env -f docker-compose.yaml pull
docker compose --env-file .env -f docker-compose.yaml up -d db
docker compose --env-file .env -f docker-compose.yaml run --rm migrate
docker compose --env-file .env -f docker-compose.yaml up -d api worker
```

检查状态：

```sh
docker compose --env-file .env -f docker-compose.yaml ps
docker compose --env-file .env -f docker-compose.yaml logs --tail=100 api worker
wget -qO- http://127.0.0.1:8000/api/v1/health
```

API 只绑定服务器的 `127.0.0.1:8000`。在 Windows 建立 SSH 隧道：

```powershell
ssh -N -L 8000:127.0.0.1:8000 用户名@服务器地址
```

然后打开 <http://127.0.0.1:8000>。

### 4. 更新版本

先确认网页中没有排队或运行中的任务，修改 `.env` 的 `APP_IMAGE`，再逐项执行：

```sh
docker compose --env-file .env -f docker-compose.yaml pull
docker compose --env-file .env -f docker-compose.yaml stop -t 90 worker api
docker compose --env-file .env -f docker-compose.yaml run --rm migrate
docker compose --env-file .env -f docker-compose.yaml up -d api worker
docker compose --env-file .env -f docker-compose.yaml ps
docker compose --env-file .env -f docker-compose.yaml logs --tail=100 api worker
```

PostgreSQL 不需要停止。不要执行 `docker compose down -v`，它会删除数据库 volume。

### 5. 回滚镜像

把 `APP_IMAGE` 改回之前验证过的 tag 或 digest，然后重新执行 `pull` 和 `up -d api worker`。数据库迁移不一定可逆，因此结构变更应保持至少一个旧应用版本兼容；破坏性迁移前必须备份。

完整操作见 [Linux 运维](docs/operations.md)。

## 使用流程

1. 在“数据管理 → 行情下载”选择自选股、沪深300历史成分股或全市场范围，设置区间与预热交易日，预览后确认下载。不超过 50 只且覆盖完整的版本会自动生成研究数据集。
2. 在“新建研究”选择数据集与股票池、因子与权重、回测区间、调仓频率和交易成本；右侧摘要实时显示校验问题和最早可回测日期。
3. 提交后在“任务中心”查看进度；worker 依次完成因子、回测和评估。
4. 在“回测”“因子评分”“因子评估”查看净值、风险指标、持仓、成交、费用、行业暴露和 IC。
5. 需要在多个因子方案之间做选择时，使用“方案比较”：按开发、验证、测试三段运行，只按验证段选方案，测试段只复核选中的方案。

检查 Tushare 权限：`uv run python scripts/probe_tushare.py --date 20250930`。

历史成分、行业和估值使用各自记录的可用时间规则。供应商无法证明严格历史可得时间的数据不会被标记为严格 PIT 认证。详细边界见 [历史时点审计](docs/temporal-execution-audit.md)。

## 数据与结果

PostgreSQL 保存市场事实、更新记录、数据发布、研究任务和结构化结果。`STORAGE_ROOT` 保存：

- `raw/`：供应商原始响应。
- `datasets/`：内容寻址的不可变研究输入。
- `runs/`：持仓、因子明细、标签和报告。
- `cache/`：可重建的 Qlib 缓存。
- `logs/`：worker 子进程日志。

每个研究结果都关联 `dataset_id`。大型 Parquet 文件在 PostgreSQL 登记相对路径、行数和 SHA-256，避免当前数据更新悄悄改变旧回测。

## 备份与恢复

没有排队或运行中的任务时：

```sh
sh scripts/backup.sh
```

备份包含 PostgreSQL dump、被研究结果实际引用的数据集和结果文件，以及 SHA-256 清单。Docker volume 负责持久化，但不能代替备份。

恢复目标必须使用空数据库和不含同名文件的 storage：

```sh
sh scripts/restore.sh /path/to/backup-directory
```

## 文档

- [系统架构](docs/architecture.md)
- [Linux 运维](docs/operations.md)
- [因子库](docs/factor-library.md)
- [调仓与交易成本](docs/rebalance-and-costs.md)
- [历史时点审计](docs/temporal-execution-audit.md)
- [行业中性化](docs/industry-neutralization.md)
- [全收益基准](docs/total-return-benchmark.md)
- [共享市场数据](docs/shared-market-data.md)
- [预热与日期校验](docs/warmup-validation.md)

## 安全边界

- `.env`、`.env.development`、Token、密码、数据和结果不得提交到 Git。
- GitHub Actions 镜像构建不使用真实数据源凭据。
- 私有 GHCR 拉取 Token 只授予 `read:packages`。
- PostgreSQL 不发布宿主机端口，API 只监听服务器回环地址。
- 项目不提供实盘下单功能。
