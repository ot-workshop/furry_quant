# Finance Quant Research Workbench

面向 A 股日线的因子研究与回测工作台：

- 官方 Tushare 获取行情与参考数据，BaoStock 抽样核验与补缺；
- PostgreSQL 保存行情事实、数据版本、任务队列与研究结果；
- 独立 worker 调用 Qlib 计算因子、回测和因子评估；
- React 网页提交研究、查看结果、管理数据。

项目仅用于研究，不连接券商交易接口，也不保证历史收益可在实盘复现。

## 目录

- [功能](#功能)
- [架构](#架构)
- [本地开发启动（Windows / 无 Docker）](#本地开发启动windows--无-docker)
- [Linux 部署（Docker Compose）](#linux-部署docker-compose)
- [使用流程](#使用流程)
- [命令速查](#命令速查)
- [常见问题](#常见问题)
- [开发与测试](#开发与测试)
- [文档索引](#文档索引)

## 功能

- 行情下载：自选股票、沪深300历史成分或全市场（沪深 A 股，含已退市）；增量补缺口、断点续传、发布不可变版本。
- 研究：固定股票池、沪深300历史成分与全市场动态条件筛选；支持板块、历史ST、上市天数、市值、成交额和历史行业条件。
- 因子与调仓：10 个行情因子和8个基本面因子，可配置窗口与权重；可选历史行业中性化（申万一级/二级/三级），日/周/月调仓。
- 交易模拟：T+1、涨跌停、停牌、整手与板块规则，佣金/印花税/过户费与默认 0.05% 冲击成本。
- 结果：净值与回撤、绩效指标、持仓、成交、费用、行业暴露、IC/Rank IC、分组收益、因子相关性。
- 财务数据：三张报表按股票增量下载，按公告时点重建可见版本；8 个基本面因子（TTM 口径）可用。
- 方案比较：开发/验证/测试三段，只按验证段选方案，测试段只复核被选中的方案。
- 可追溯：每个结果记录数据版本、冻结数据集、代码与依赖哈希；PostgreSQL 与文件可一致备份恢复。

## 架构

```text
浏览器 (React)
   │  HTTP
   ▼
API 进程  ──────────────►  PostgreSQL
python -m finance_quant      ├─ 行情事实、数据发布与修订
  .server api                ├─ 任务队列（研究 / 方案比较 / 行情下载）
   │ 只提交与查询任务         └─ 研究结果元数据
   │
Worker 进程 ─ 每个任务一个子进程（Qlib 只在这里运行）
python -m finance_quant.server worker
   │
   └─ STORAGE_ROOT/
       ├─ data/      共享行情抓取、研究数据集、沪深300历史成分视图、财务数据、申万行业
       ├─ datasets/  冻结的研究输入（内容寻址）
       ├─ runs/      Parquet 明细与报告
       ├─ network/   接口调用配额记录
       └─ logs/      每个任务的子进程日志
```

- API 不运行任何计算；所有研究、方案比较和行情下载都排队交给 worker 串行执行。
- 同一数据库只允许一个 worker（PostgreSQL advisory lock 保证）。
- 研究运行时读取冻结后的输入，行情后续更新不会改变已有结果。

详细设计见 [docs/architecture.md](docs/architecture.md)。

## 本地开发启动（Windows / 无 Docker）

本地开发需要同时运行三个进程：**API**、**worker**、**前端开发服务器**。

### 0. 前置软件

| 软件 | 版本 | 用途 |
| --- | --- | --- |
| Python | 3.11 或 3.12 | 后端 |
| [uv](https://docs.astral.sh/uv/) | 0.10+ | Python 依赖管理 |
| Node.js | 22 | 前端 |
| PostgreSQL | 16+（推荐 18） | 数据库 |
| Tushare Token | 官方账号 | 下载行情 |

### 1. 安装依赖

```powershell
uv sync --locked --extra dev
npm.cmd --prefix frontend ci --no-audit --no-fund
```

### 2. 创建开发数据库

用 PostgreSQL 管理账户（如 `psql -U postgres`）执行：

```sql
CREATE ROLE quant_dev WITH LOGIN PASSWORD '设置一个开发密码';
CREATE DATABASE finance_quant_dev OWNER quant_dev;
```

### 3. 配置环境变量

```powershell
Copy-Item .env.development.example .env.development
```

编辑 `.env.development`，至少填写：

```dotenv
TUSHARE_TOKEN=你的官方Token
POSTGRES_PASSWORD=上一步设置的密码
```

其余默认值说明：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `POSTGRES_HOST/PORT/DB/USER` | `127.0.0.1` / `5432` / `finance_quant_dev` / `quant_dev` | 开发库连接 |
| `STORAGE_ROOT` | `./storage-dev` | 所有数据与结果文件的根目录 |
| `FINANCE_QUANT_RUNS` / `FINANCE_QUANT_DATA` | `STORAGE_ROOT/runs`、`STORAGE_ROOT/data` | 可单独覆盖 |
| `FINANCE_QUANT_QUOTA_DB` | `STORAGE_ROOT/network/quota.sqlite3` | 接口配额记录 |
| `ALLOWED_ORIGINS` | 5173 与 8000 端口 | 允许提交任务的网页来源，**必须包含你实际打开的地址** |

> 开发库、`storage-dev/` 与生产环境必须相互独立。`.env.development` 不要提交到 Git。

### 4. 初始化数据库结构（只需一次，升级后再执行一次）

```powershell
uv run python -m finance_quant.server migrate --env-file .env.development
uv run python -m finance_quant.server check   --env-file .env.development
```

`check` 输出 `Database schema and storage are ready` 即可。

### 5. 启动三个进程（各开一个终端）

终端 1 — API：

```powershell
uv run python -m finance_quant.server api --env-file .env.development --reload --host 127.0.0.1 --port 8000
```

终端 2 — worker（不启动它，任务会一直“排队中”）：

```powershell
uv run python -m finance_quant.server worker --env-file .env.development
```

终端 3 — 前端开发服务器：

```powershell
npm.cmd --prefix frontend run dev
```

打开 <http://127.0.0.1:5173>。Vite 会把 `/api` 代理到 `127.0.0.1:8000`，修改前端代码会热更新。

**不需要改前端时**可以只构建一次，由 API 直接托管页面：

```powershell
npm.cmd --prefix frontend run build
```

然后打开 <http://127.0.0.1:8000>（无需终端 3）。此时 `ALLOWED_ORIGINS` 需包含 `http://127.0.0.1:8000`。

### 6. 验证

- <http://127.0.0.1:8000/api/v1/health> 返回 `{"status":"ok", ...}`。
- 网页“工作台”能打开；页面右上角“任务”显示 0。
- 可选：检查 Tushare 权限 `uv run python scripts/probe_tushare.py --date 20250930`（结果写入 `storage/tushare-probe/`，不回显 Token）。

## Linux 部署（Docker Compose）

生产环境使用 GitHub Actions 构建的应用镜像，服务器通过 Docker Compose 运行数据库、API 和 worker。数据库初始化、目录权限准备和表结构迁移均由启动流程完成。

### 部署要求

- Linux x86_64，已安装 Docker Engine 和 Docker Compose v2.26 或更新版本。
- 部署用户具有 Docker 操作权限。
- 能够访问应用镜像仓库；私有 GHCR 镜像需要具备 `read:packages` 权限的 GitHub Token。

服务器无需安装 Python、Node.js 或构建工具，也无需克隆完整源码。部署文件为同一版本的 [docker-compose.yaml](docker-compose.yaml) 和 [.env.example](.env.example)。

### 配置部署环境

将两个部署文件放入服务器上的独立目录，然后创建环境配置：

```sh
mkdir -p ~/finance-quant
cd ~/finance-quant
cp .env.example .env
chmod 600 .env
```

在 `.env` 中填写应用镜像、数据库密码和数据源 Token：

```dotenv
APP_IMAGE=ghcr.io/quqionfree/my_quant_project:sha-实际提交号
POSTGRES_USER=quant
POSTGRES_DB=finance_quant
POSTGRES_PASSWORD='替换为数据库密码'
TUSHARE_TOKEN=替换为官方TushareToken

STORAGE_ROOT=./storage
BACKUP_ROOT=./backups
RESTORE_ROOT=./restores
```

| 配置项 | 说明 |
| --- | --- |
| `APP_IMAGE` | 应用镜像的 `sha-*` 标签或 digest，来自 GitHub Actions 构建结果 |
| `POSTGRES_USER` / `POSTGRES_DB` | 首次初始化时创建的用户和数据库，默认 `quant` / `finance_quant` |
| `POSTGRES_PASSWORD` | 数据库密码；包含 `$`、`#` 等字符时使用单引号包裹 |
| `TUSHARE_TOKEN` | 行情和财务数据下载所需的官方 Tushare Token；未配置时仍可启动服务 |
| `STORAGE_ROOT` | 行情、财务、行业、下载缓存和回测结果的主机目录，默认 `./storage` |
| `BACKUP_ROOT` / `RESTORE_ROOT` | 备份归档和独立恢复环境的主机目录，默认 `./backups` / `./restores` |
| `HTTP_PORT` | API 的主机端口，默认 `8000`，绑定至 `127.0.0.1` |

三个数据目录支持相对路径和绝对路径。Compose 创建挂载目录后，`storage-init` 准备挂载根目录的写权限；API 和 worker 以非 root 用户运行。

### 登录镜像仓库并启动

登录私有 GHCR 镜像仓库，在密码提示中输入具有镜像读取权限的 Token：

```sh
docker login ghcr.io -u YOUR_GITHUB_USERNAME
```

在部署目录执行：

```sh
docker compose pull
docker compose up -d
docker compose ps -a
```

首次启动时，PostgreSQL 官方镜像按 `.env` 创建用户和数据库。`migrate` 等待数据库通过真实密码登录检查、目录准备完成后执行 Alembic 迁移；API 和 worker 在迁移成功后启动。

| 服务 | 正常状态 | 职责 |
| --- | --- | --- |
| `db` | `healthy` | PostgreSQL 数据库 |
| `storage-init` | `Exited (0)` | 一次性准备主机挂载目录权限 |
| `migrate` | `Exited (0)` | 应用尚未执行的数据库迁移 |
| `api` | `healthy` | 网页与 HTTP API |
| `worker` | `running` | 串行执行数据和研究任务 |

重复执行 `docker compose up -d` 会复用现有数据库和主机目录。准备或迁移失败时，相关依赖服务不会启动；使用 `docker compose logs db storage-init migrate` 查看失败阶段。

### 访问工作台

默认地址为服务器本机的 <http://127.0.0.1:8000>，PostgreSQL 不发布主机端口。远程访问可建立 SSH 隧道：

```sh
ssh -N -L 8000:127.0.0.1:8000 USER@SERVER
```

随后在本机浏览器打开 <http://127.0.0.1:8000>。通过域名或反向代理访问时，将实际网页来源加入 `.env` 的 `ALLOWED_ORIGINS`。

验证数据库结构与存储连接：

```sh
docker compose exec api python -m finance_quant.server check
```

### 更新应用

确认任务中心没有运行中的任务，将 `.env` 的 `APP_IMAGE` 更新为目标版本，然后执行：

```sh
docker compose pull
docker compose stop -t 90 worker api
docker compose up -d
docker compose ps -a
```

新镜像对应的迁移会自动执行，数据库卷和主机数据目录继续复用。Compose 文件有变化时，应同步使用目标版本的文件。应用镜像回滚不会回滚数据库结构。

### 数据管理与运维

数据库存放在本 Compose 项目的 `postgres` 持久化卷中；业务文件保存在 `.env` 指定的三个主机目录中。日常备份和恢复可通过“数据管理 → 备份与恢复”操作。

目录布局、日志排查、完整备份、独立环境恢复和试运行环境重建见 [Linux 运维文档](docs/operations.md)。

## 使用流程

**统一更新数据**：进入“数据管理 → 统一更新”，选择股票范围、日期和预热交易日，勾选行情、财务、行业与基准，预览后确认。财务使用本次发布行情的股票清单；失败后可续传，已成功项目不会重跑。自动更新按北京时间运行，需要 worker 持续在线；保存自动计划时结束日期会随数据日推进。恢复备份后自动计划默认关闭。

1. **下载行情**：数据管理 → 行情下载。选择范围（自选股票 / 沪深300历史成分 / 全市场）、研究区间和向前预热天数，点击“预览下载方案”查看可复用与需下载的数据量，再“确认下载”。下载在 worker 中执行，可暂停、续传。
   - 不超过 50 只且覆盖完整的版本会自动生成**研究数据集**（数据管理 → 研究数据集）。
   - 完整的沪深300版本可用于“历史成分（动态股票池）”研究。
   - 全市场或自选范围可用于“全市场与条件筛选”；使用历史上市、退市与名称记录逐日判断资格，详见[动态股票池教程](docs/dynamic-universe.md)。
2. **新建研究**：选择数据集与股票池、因子与权重、回测区间、持股数、调仓频率和交易成本。右侧摘要实时列出所有问题，并给出“最早可回测日期”（之前的数据用于因子预热）。
3. **查看进度**：提交后自动跳转到任务中心；完成后可直接打开回测、因子评分、因子评估。
4. **分析结果**：回测详情的“概览”点击净值曲线上某一天可直接查看当日持仓；“参数与口径”列出全部参数、已知限制和数据时点核验。
5. **方案比较**：方案比较 → 新建。设置总区间和验证段、测试段的开始日期，添加 2–8 个因子方案；系统只按验证段收益差选方案并锁定，再运行一次测试段。
6. **复用配置**：任务中心的“复用配置”把历史任务载入新建研究页。

## 命令速查

所有服务入口都是 `python -m finance_quant.server <role>`，本地开发加 `--env-file .env.development`：

| 命令 | 作用 |
| --- | --- |
| `migrate` | 执行数据库迁移（等同 `alembic upgrade head`） |
| `check` | 检查数据库结构与存储目录 |
| `api [--host --port --reload]` | 启动 HTTP API（同时托管已构建的前端） |
| `worker` | 启动任务 worker（每个部署只能有一个） |

其他脚本：

| 脚本 | 作用 |
| --- | --- |
| `scripts/probe_tushare.py` | 检查 Tushare Token 可用的接口 |
| `scripts/check_official_benchmark.py` | 下载并核对官方基准指数 |
| `scripts/check_official_industries.py` | 下载历史申万行业（启用行业中性化需要） |
| `scripts/audit_temporal_sources.py` | 审计估值数据的时点来源 |
| `python -m finance_quant.backup create/verify/restore` | 完整备份、校验与空环境恢复 |

## 常见问题

**启动报 `Database has no schema yet` / `关系 "alembic_version" 不存在`**
原生开发环境执行 `uv run python -m finance_quant.server migrate --env-file .env.development`。Docker Compose 自动迁移，先查看 `docker compose logs migrate`，处理失败原因后重新 `docker compose up -d`。

**Docker 报 `password authentication failed`**
检查 `.env` 是否与该数据卷首次初始化时的账号密码一致。修改 `.env` 不会改写已有数据库的密码。确认试运行数据库可丢弃时，可在该项目目录执行 `docker compose down --volumes`，再 `docker compose up -d` 重新初始化；主机挂载目录不会被删除。正式数据应保留原凭据或进行明确的密码变更。

**报 `POSTGRES_HOST is required`**
没有加 `--env-file .env.development`，或该文件缺少 `POSTGRES_HOST`。

**任务一直“排队中”**
worker 没有启动，或启动报错。查看 worker 终端；每个任务的详细日志在 `STORAGE_ROOT/logs/<任务ID>.log`。

**提交时提示“请从本机研究工作台提交任务”**
当前浏览器地址不在 `ALLOWED_ORIGINS` 中。把实际打开的地址（例如 `http://127.0.0.1:8000`）加入后重启 API。

**新建研究页提示“还没有可用于研究的数据集”**
在数据管理 → 行情下载中完成一个范围后，到“研究数据集”查看：不超过50只且覆盖完整的范围可生成固定池，沪深300可生成历史成分池，全市场和自选范围可生成动态条件池。已有行情但缺少历史名称参考数据时，对同一范围执行一次增量下载，复用行情并补齐参考数据。

**worker 报 `Another worker already owns this database queue`**
同一数据库已有一个 worker 在运行，关闭多余的那个。

**下载提示配额或被暂停**
Tushare / BaoStock 调用按 `FINANCE_QUANT_QUOTA_DB` 计数限速；已完成的部分会保留，第二天或解除限制后点击“续传”。

## 开发与测试

```powershell
uv run ruff check src alembic tests scripts
uv run ruff format --check src alembic tests scripts
uv run mypy
uv run pytest -q                       # 单元测试（不需要数据库）
npm.cmd --prefix frontend test
npm.cmd --prefix frontend run format:check
npm.cmd --prefix frontend run build
```

PostgreSQL 集成测试需要一个可清空的测试库：

```powershell
$env:FINANCE_QUANT_TEST_POSTGRES = "1"   # 并设置 POSTGRES_* 指向测试库
uv run pytest -q tests/integration
```

CI（`.github/workflows/full-tests.yml`）在 Pull Request 上运行以上全部检查与迁移；`docker.yml` 在推送 `main` 或 tag 时构建镜像。

## 文档索引

- [系统架构](docs/architecture.md)
- [Linux 运维](docs/operations.md)
- [前端结构与接口](docs/frontend-contract.md)
- [因子库](docs/factor-library.md)
- [财务数据](docs/financial-data.md)
- [行业数据](docs/industry-data.md)
- [调仓与交易成本](docs/rebalance-and-costs.md)
- [共享行情数据](docs/shared-market-data.md)
- [全市场动态股票池与筛选](docs/dynamic-universe.md)
- [预热与日期校验](docs/warmup-validation.md)
- [历史时点审计](docs/temporal-execution-audit.md)
- [行业中性化](docs/industry-neutralization.md)
- [全收益基准](docs/total-return-benchmark.md)

## 安全边界

- `.env`、`.env.development`、Token、密码、数据和结果不提交到 Git。
- 镜像构建不使用真实数据源凭据；GHCR 拉取 Token 只授予 `read:packages`。
- PostgreSQL 不发布宿主机端口，API 只监听回环地址；提交任务要求本机来源。
- 项目不提供实盘下单功能。
