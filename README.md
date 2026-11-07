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
- 研究：10 个可配置因子（动量、反转、波动、估值、流动性、回撤、市值等）、可选历史行业中性化、日/周/月调仓。
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
       ├─ data/      共享行情抓取、研究数据集、沪深300历史成分视图、财务数据
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

服务器只需要 Docker Engine、Docker Compose v2、`docker-compose.yaml`、`.env` 和 `scripts/`；镜像由 GitHub Actions 构建并推送到私有 GHCR。

### 1. 准备目录

```sh
mkdir -p ~/finance-quant && cd ~/finance-quant
# 从本地复制：docker-compose.yaml、.env.example、scripts/backup.sh、scripts/restore.sh
cp .env.example .env && chmod 600 .env
mkdir -p storage backups
```

`.env` 填写：

```dotenv
APP_IMAGE=ghcr.io/quqionfree/my_quant_project:sha-实际提交号
TUSHARE_TOKEN=官方Token
POSTGRES_PASSWORD=独立强密码
STORAGE_ROOT=/home/用户名/finance-quant-data/storage
BACKUP_ROOT=/home/用户名/finance-quant-data/backups
```

### 2. 登录私有镜像仓库

使用只授予 `read:packages` 的 classic personal access token（不要写入 `.env`）：

```sh
read -s GHCR_TOKEN
echo "$GHCR_TOKEN" | docker login ghcr.io -u GitHub用户名 --password-stdin
unset GHCR_TOKEN
```

### 3. 首次启动

```sh
docker compose --env-file .env config            # 检查配置
docker compose --env-file .env pull
docker compose --env-file .env up -d db
docker compose --env-file .env run --rm migrate  # 初始化数据库结构
docker compose --env-file .env up -d api worker
docker compose --env-file .env ps
wget -qO- http://127.0.0.1:8000/api/v1/health
```

API 只监听服务器的 `127.0.0.1:8000`，PostgreSQL 不暴露端口。从 Windows 通过 SSH 隧道访问：

```powershell
ssh -N -L 8000:127.0.0.1:8000 用户名@服务器地址
```

然后打开 <http://127.0.0.1:8000>。

### 4. 升级

先确认网页“任务中心”没有排队或运行中的任务，修改 `.env` 的 `APP_IMAGE` 后：

```sh
docker compose --env-file .env pull
docker compose --env-file .env stop -t 90 worker api
docker compose --env-file .env run --rm migrate
docker compose --env-file .env up -d api worker
docker compose --env-file .env logs --tail=100 api worker
```

不要执行 `docker compose down -v`（会删除数据库 volume）。回滚：把 `APP_IMAGE` 改回之前的 tag 后重新 `pull` 与 `up -d api worker`；破坏性迁移前务必先备份。

### 5. 备份与恢复

没有运行中的任务时：

```sh
sh scripts/backup.sh                        # PostgreSQL dump + 被引用的数据集与结果 + SHA-256 清单
sh scripts/restore.sh /path/to/backup-dir   # 目标必须是空数据库与空 storage
```

完整运维说明见 [docs/operations.md](docs/operations.md)。

## 使用流程

1. **下载行情**：数据管理 → 行情下载。选择范围（自选股票 / 沪深300历史成分 / 全市场）、研究区间和向前预热天数，点击“预览下载方案”查看可复用与需下载的数据量，再“确认下载”。下载在 worker 中执行，可暂停、续传。
   - 不超过 50 只且覆盖完整的版本会自动生成**研究数据集**（数据管理 → 研究数据集）。
   - 完整的沪深300版本可用于“历史成分（动态股票池）”研究。
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
| `scripts/backup.sh` / `scripts/restore.sh` | 一致性备份与恢复 |

## 常见问题

**启动报 `Database has no schema yet` / `关系 "alembic_version" 不存在`**
数据库还没迁移。执行 `uv run python -m finance_quant.server migrate --env-file .env.development`（Docker：`docker compose run --rm migrate`）。

**报 `POSTGRES_HOST is required`**
没有加 `--env-file .env.development`，或该文件缺少 `POSTGRES_HOST`。

**任务一直“排队中”**
worker 没有启动，或启动报错。查看 worker 终端；每个任务的详细日志在 `STORAGE_ROOT/logs/<任务ID>.log`。

**提交时提示“请从本机研究工作台提交任务”**
当前浏览器地址不在 `ALLOWED_ORIGINS` 中。把实际打开的地址（例如 `http://127.0.0.1:8000`）加入后重启 API。

**新建研究页提示“还没有可用于研究的数据集”**
先在数据管理 → 行情下载中下载自选股票（不超过 50 只），完成后才会生成数据集。

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
- [调仓与交易成本](docs/rebalance-and-costs.md)
- [共享行情数据](docs/shared-market-data.md)
- [预热与日期校验](docs/warmup-validation.md)
- [历史时点审计](docs/temporal-execution-audit.md)
- [行业中性化](docs/industry-neutralization.md)
- [全收益基准](docs/total-return-benchmark.md)

## 安全边界

- `.env`、`.env.development`、Token、密码、数据和结果不提交到 Git。
- 镜像构建不使用真实数据源凭据；GHCR 拉取 Token 只授予 `read:packages`。
- PostgreSQL 不发布宿主机端口，API 只监听回环地址；提交任务要求本机来源。
- 项目不提供实盘下单功能。
