# 量化研究工作台

基于 Qlib 的本地 A 股日线研究项目。前端查看股票因子、横截面排名和回测结果，Python 后端管理结果与运行 Qlib。

## 当前可用

- 已联合核验五股（浦发、平安银行、茅台、招商、五粮液）2025-01-01至2026-09-30的行情，共424个交易日、2120条股票日线；四股价格和复权回退至BaoStock，茅台保留Tushare，逐股来源可查。
- Qlib 0.9.7 实际执行2025-03-03至2026-09-30的回测，共388个交易日，使用前一交易日评分、下一交易日开盘成交。
- 真实行情使用动量、盈利收益率估值和低波动三个因子；原有合成样本单独保留。支持按日期查看原始值、百分位、综合分和历史变化。
- 回测净值、明确来源的基准、回撤、费用、逐日持仓、成交、参数及 JSON 下载。
- 配置化数据与回测、分片缓存续传、数据覆盖与复权连续性检查，以及网页“数据管理”。
- 因子选择、窗口与权重配置；Qlib IC/Rank IC、分组平均收益、因子相关性和覆盖率评估。网页“因子评估”支持查看结果及下载 JSON/Markdown 报告。
- 历史开发/验证/测试分段与四方案比较：仅按验证区间选方案，保存选择记录后才运行选中方案的测试区间；网页“方案比较”与独立报告可查。
- 只读 FastAPI 接口、React 前端；构建后同源访问。
- 结果不可变保存，代码/依赖/Python 版本指纹防止误用旧结果。

**页面区分真实行情和合成样本；当前五股区间用于工程验收。原Tushare跨年候选及异常试算继续隔离，新增联合方案已单独发布；估值、涨跌停和指数仍来自Tushare兼容服务，联合核验不代表财报或全市场已认证。市场数据库、真实财报因子和网页任务提交尚未接入。**

联合规则、证据及配置见[联合数据核验](docs/joint-data-validation.md)。已激活联合配置，现有环境直接运行 `.\start.ps1` 即可查看；原短区间配置和数据保留。

## 运行

已在 Windows + Python 3.12.10、Node.js 环境验证。使用 uv 管理独立 Python 环境，不修改系统 Python。

在项目根目录：

```powershell
uv sync --extra dev
npm.cmd --prefix frontend install --no-audit --no-fund
npm.cmd --prefix frontend run build
.\.venv\Scripts\python.exe -m finance_quant.cli demo
.\.venv\Scripts\python.exe -m finance_quant.cli serve
```

打开 <http://127.0.0.1:8000>；接口文档为 <http://127.0.0.1:8000/docs>。默认仅监听本机。启动服务的终端按 Ctrl+C 停止。

真实数据流程：本地 `.env` 中设置 `TUSHARE_TOKEN`、`TUSHARE_HTTP_URL`（参考 `.env.example`）；密钥只供后端使用且已被版本管理忽略。推荐通过配置文件指定股票池、日期和回测参数：

```powershell
.\.venv\Scripts\python.exe -m finance_quant.cli probe
.\.venv\Scripts\python.exe -m finance_quant.cli plan --config configs\research.joint.json
.\.venv\Scripts\python.exe -m finance_quant.cli download --config configs\research.joint.json
.\.venv\Scripts\python.exe -m finance_quant.cli status --config configs\research.joint.json
.\.venv\Scripts\python.exe -m finance_quant.cli real --config configs\research.joint.json
.\.venv\Scripts\python.exe -m finance_quant.cli evaluate --config configs\research.joint.json
.\.venv\Scripts\python.exe -m finance_quant.cli experiment --config configs\research.joint.json --plan configs\experiment.historical.json
.\start.ps1 -Config configs\research.joint.json
```

`probe` 与 `download` 访问服务；`plan`、`status`、`real`、`evaluate` 完全离线，`serve` 只读结果。下载按年度边界复用行情分片、逐月校验，漏行可按实际交易日补抓；缺失或复权异常不发布。历史修订可加 `--refresh`，保留旧快照。实际权限不等同于官方积分权益。`configs/research.two-year.json` 保留作多年诊断配置，目前数据质量未通过，不能当作已验收案例。详见 [数据管理](docs/data-management.md) 与 [数据问题证据](docs/provider-data-issues.md)。

环境和前端已经准备好后，可以直接运行：

```powershell
.\start.ps1
```

该脚本优先使用最近一次成功配置回测保存的 `runs/active-research.json`，或用 `-Config` 指定文件；配置模式会生成或复用回测及因子评估，再启动网页。尚无激活配置时保留原真实小样本/合成样本流程。可用 `.\start.ps1 -Demo` 显式运行合成样本。启动时不会自动下载数据。配置路径相对于配置文件解析，禁止混用同类命令行覆盖项。

因子和评估参数的说明见 [因子研究](docs/factor-research.md)。默认方案保持原有三个因子和权重；可选择其中一部分，动量和波动率支持配置历史窗口。历史分段实验使用本地快照离线运行，规则、报告和限制见 [方案比较](docs/historical-experiments.md)。网页目前只读，图形化配置与任务提交留待后续。

前端开发可在另一个终端运行 `npm.cmd --prefix frontend run dev`，访问其输出的本地地址；`/api` 已代理至 8000 端口。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q
npm.cmd --prefix frontend run build
```

测试检查实际 Qlib 成交价、信号滞后、最低费用、持仓股数、现金与资产逐日对账、原始本金计算收益、未来数据隔离、结果不可变及 API 分页/错误行为。

Qlib 的旧 Gym 依赖和部分数值库会产生兼容性/弃用提示，当前样本测试通过；这些提示不代表生产市场规则已验收。`uv.lock` 和前端 lockfile 固定本次验证环境。CLI 默认限制数值库线程，适配本地运行资源。

## 数据与运行记录

- `data/sample-qlib/`：合成原始 CSV、Qlib 本地日历、标的列表与特征文件。
- `data/tushare/20260803-20260930/raw/`：真实原始响应 Parquet、无密钥请求元数据、来源标识、时间及校验和。
- `data/tushare/20260803-20260930/snapshots/{version}/`：不可变标准行情、指数、交易日历及清单；Qlib 特征按代码版本隔离。
- `data/managed/`：可配置股票池的原始分片、覆盖报告、不可变快照及SQLite下载记录；不是PostgreSQL市场数据库。
- `data/managed-joint/`：联合数据、逐股核验报告、行级来源，以及`secondary/baostock/`中的原始双口径行情缓存。
- `runs/factors/{id}/result.json`：一次固定评分批次，含原始值、处理分数、日期、权重及来源。
- `runs/backtests/{id}/result.json`：Qlib 结果、持仓、成交、原生报告及运行配置。
- `runs/evaluations/{id}/result.json` 与 `report.md`：逐日 IC、分组收益、标签明细、相关性与可独立阅读的报告。
- `runs/experiments/{id}/`：冻结计划、验证期选择记录、各段子任务引用、校验和及比较报告。
- 样本 ID 包含运行指纹；同版本重跑复用已有结果，代码或依赖改变后产生不同 ID。历史查询绑定评分批次，不混合版本。
- 默认路径相对于运行目录，应从项目根目录启动；CLI 可用 `--runs-dir` 和 `--data-dir` 指定位置。API 工厂也可显式指定结果目录。
- 每次样本数据导出在 `data/sample-qlib/{运行指纹}/` 下保留独立特征文件；不会覆盖其他版本的快照。
- 密钥不应写入源码、运行产物、前端或日志；Tushare适配器读取后端本地配置。

合成样本基准是合成价格均值；真实回测基准是沪深300价格指数。真实策略按Qlib复权收益/等效持股口径计算，现金分红不另记现金；与价格指数基准的分红口径不同。开盘触及买入涨停价或卖出跌停价时保守不成交；尚未支持停牌缺口、退市和逐笔分红账本。每次运行详情会保留这些假设。没有大规模下载、生产数据库迁移或实盘操作。

## 项目结构

```text
frontend/                       React + TypeScript + ECharts
src/finance_quant/
  api.py                        结果查询接口与本地前端托管
  cli.py                        接口探测、下载、样本/真实计算、服务启动
  configuration.py              股票池、数据与回测配置校验
  sample.py                     明确的合成数据与纯因子函数
  storage.py                    不可变本地结果存储
  integrations/qlib_demo.py      Qlib 数据导出、原生回测、结果转换
  integrations/qlib_runner.py    样本与真实数据共用的Qlib执行适配
  data/providers/               Tushare配置、限流重试、字段与单位转换
  data/market.py                真实下载缓存、质量检查、不可变快照
  data/managed.py               分片续传、缺失补抓与快照发布
  data/catalog.py               SQLite下载状态与只读查询
  research/market_factors.py    真实行情的因子计算
  research/registry.py          因子与评估参数校验
  research/evaluation.py        Qlib IC、收益标签与分组诊断
  research/report.py            因子评估报告
  application/factor_workflow.py 共享的不可变评分批次
  application/evaluation_workflow.py 离线评估与报告发布
  application/real_workflow.py  本地快照到Qlib结果的完整流程
tests/                          小样本验收与 API 测试
docs/                           完整架构、接口约定与验证记录
```

第三方接入首轮状态见 [真实数据接入记录](docs/real-data-status.md)。后续为财报版本验收、历史股票池、正式数据库、旧因子迁移和网页任务提交，详见 [架构规划](docs/architecture-plan.md)。

已补充财报公告/修订的时点函数与反例测试，真实财报接入尚未完成；已发现并修正的时间边界问题见 [专项检查](docs/lookahead-audit.md)。
