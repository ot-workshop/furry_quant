# 量化研究工作台

基于 Qlib 的本地 A 股日线研究项目。前端配置研究、提交后台任务并查看股票因子和回测结果，Python 后端管理数据、任务和 Qlib 计算。

## 当前可用

- 已核验历史成分、行业、PE/PB和日线成交口径；修复估值日期载入、信号可用时间、固定池数量上限及近整仓卖出余股问题。评分、回测、因子评估页面分别显示“数据时点与成交边界”，无法认证的历史发布时间与实际成交容量明确保留限制。598项后端、50项前端及原多年398笔成交复跑通过；旧结果不重写。见[本轮核验](docs/temporal-execution-audit.md)。
- “数据管理 → 共享行情与增量更新”支持沪深300历史成分、全市场（沪深A股）和自选范围，配置起止日期与预热，预览覆盖后确认下载；共用行情，只补缺口，支持暂停续传、冲突隔离及不可变版本。完整沪深300版本可选择动态研究，小范围合格版本可作为固定池快照研究。旧仓库与回测保留；全市场批量下载未启动。见[共享行情使用与边界](docs/shared-market-data.md)。

- 沪深300历史数据仓：官方主源、BaoStock串行抽样核验及异常补缺，493只历史相关股票全部完成检查（440只单源、53只双源）。研究范围2020-01-02至2026-10-30，127日预热起点2019-06-28，保留83期成分变化和逐股处理报告。见[数据仓说明](docs/csi300-warehouse.md)和[自动处理报告](docs/automatic-download-handling.md)。
- 网页选择“沪深300历史成分股”即可运行动态池因子、Qlib回测和评估；调入前行情只预热，不提前参与排名。调出持仓持续尝试退出，停牌/跌停受阻保留持仓和记录；接入科创板数量、T+1、上市初期限制等日线交易规则。多年明细逐日写入Parquet，网页按日期/股票读取。成分采用月度快照下一交易日口径，精确公告时点未认证。见[历史动态研究使用及边界](docs/historical-index-research.md)。

- 当前新下载主源已切换官方Tushare，BaoStock保留串行核验和整段回退。原15股官方数据完成908个数据日、665个回测日及十因子评估，15股全部保留官方来源、无回退，复核结果可在网页任务列表打开。见[切换验收](docs/official-tushare-switch.md)。

- 网页“新建研究”：选择股票池和数据快照、勾选因子、设置窗口与权重，调整回测参数后直接运行；支持后台队列、阶段进度、错误反馈、配置复用及对应结果跳转。也可提交新股票池下载。见[网页研究使用说明](docs/web-research.md)。
- 支持日/周/月调仓；高级设置可选择分项费用，配置券商佣金、最低单笔佣金、历史或自定义卖出印花税/双边过户费及双边冲击成本。回测展示逐笔和累计费用，所有费用实际计入Qlib账户；旧配置沿用综合费率。见[调仓与费用口径](docs/rebalance-and-costs.md)。
- “回测基础 → 评价基准”可选择官方沪深300全收益指数H00300.CSI；已核验2004-12-31至2026-10-30共5300条点位，交易日无缺口。切换基准复用同一策略执行，保留成交、持仓及策略净值，独立保存基准与相对净值收益、跟踪误差和信息比率，旧结果不重写。见[全收益基准](docs/total-return-benchmark.md)；已支持可关闭的[历史行业中性化](docs/industry-neutralization.md)、处理前后明细及持仓行业暴露；行业持仓约束留在[后续计划](docs/industry-neutralization-plan.md)。
- 新下载默认按回测日期和因子窗口自动倒推预热起止；点击“预览下载方案”，确认区间和参数后才下载行情。复权缺失经BaoStock整段核验与回退，连接失败有限重试，实际阻断原因在任务明细中显示。见[自动预热](docs/warmup-validation.md)和[复权恢复](docs/adjustment-recovery.md)。
- 已复核表单改动、真实Qlib任务与结果批次跳转，参数生效及修复记录见[网页流程复核](docs/web-workflow-review.md)。
- 已联合核验五股（浦发、平安银行、茅台、招商、五粮液）2025-01-01至2026-09-30的行情，共424个交易日、2120条股票日线；四股价格和复权回退至BaoStock，茅台保留Tushare，逐股来源可查。
- Qlib 0.9.7 实际执行2025-03-03至2026-09-30的回测，共388个交易日，使用前一交易日评分、下一交易日开盘成交。
- 真实行情支持10项可选指标，默认保留动量、盈利收益率和低波动三项；支持同类多窗口实例及独立权重。股票页可查看原始值、百分位、贡献、公式、覆盖率和缺失原因。口径与使用见[十项因子](docs/factor-library.md)；原有合成样本单独保留。
- 回测净值、明确来源的基准、回撤、费用、逐日持仓、成交、参数及 JSON 下载。
- 配置化数据与回测、分片缓存续传、数据覆盖与复权连续性检查，以及网页“数据管理”。
- 因子选择、窗口与权重配置；Qlib IC/Rank IC、分组平均收益、因子相关性和覆盖率评估。网页“因子评估”支持查看结果及下载 JSON/Markdown 报告。
- 历史开发/验证/测试分段与四方案比较：仅按验证区间选方案，保存选择记录后才运行选中方案的测试区间；网页“方案比较”与独立报告可查。
- FastAPI 结果查询与本机研究任务接口、React 前端；构建后同源访问。
- 结果不可变保存，代码/依赖/Python 版本指纹防止误用旧结果。

**页面区分真实行情和合成样本。旧第三方数据及异常试算继续保留与隔离；当前新下载和历史仓库使用官方Tushare，单源内部检查与双源核验分开标记。精确成分公告时点、估值历史修订和财报可用时点尚未认证，尚未接入真实财报因子或券商逐笔分红账本。**

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

`probe` 与 `download` 访问服务；`plan`、`status`、`real`、`evaluate`、`experiment` 完全离线；`serve` 提供本机任务提交与结果查询。下载按年度边界复用行情分片、逐月校验，漏行可按实际交易日补抓；缺失或复权异常不发布。历史修订可加 `--refresh`，保留旧快照。实际权限不等同于官方积分权益。`configs/research.two-year.json` 保留作多年诊断配置，目前数据质量未通过，不能当作已验收案例。详见 [数据管理](docs/data-management.md) 与 [数据问题证据](docs/provider-data-issues.md)。

环境和前端已经准备好后，可以直接运行：

```powershell
.\start.ps1
```

该脚本默认直接启动网页，优先使用 `runs/active-research.json` 或 `-Config` 指定的数据与结果目录；不会在打开网页前运行回测或评估。计算请在网页“新建研究”提交。如需先计算配置中的研究再启动，可运行 `.\start.ps1 -RunResearch -Config configs\research.joint.json`；`.\start.ps1 -Demo` 显式生成合成样本。启动不自动下载数据。配置路径相对于配置文件解析，禁止混用同类命令行覆盖项。

看到 `Uvicorn running on http://127.0.0.1:8000` 后，在浏览器打开该地址；终端持续运行以提供服务，按Ctrl+C停止。端口被占用时请关闭旧服务，或使用 `.\start.ps1 -Port 8001`。Windows Python 3.12的系统信息查询兼容处理由CLI及任务进程自动启用，排查与验收见[启动说明](docs/startup.md)。

因子和评估参数的说明见 [因子研究](docs/factor-research.md)。网页可选择现有因子并设置权重与窗口、提交普通研究任务；历史分段方案比较仍通过CLI配置运行，规则和限制见 [方案比较](docs/historical-experiments.md)。

固定池回测支持沪深主板和创业板A股；沪深300历史池模式另支持科创板及其数量规则。北交所、B股、ETF及股票池中的指数仍明确拒绝。新股票池、停牌、扩缩池与异常恢复的实际验收见 [多股票池流程复核](docs/pool-workflow-review.md)，历史动态池见[动态研究说明](docs/historical-index-research.md)。

回测页沿用旧版“年度收益图 + 绩效指标表”，补齐年化收益、年化波动率、夏普、卡玛、日收益胜率/盈亏比和最大回撤日，并列展示基准；旧结果无需重跑即可查看补算指标。公式、252日/3%假设和旧结果兼容方式见 [回测绩效](docs/backtest-performance.md)。

前端开发可在另一个终端运行 `npm.cmd --prefix frontend run dev`，访问其输出的本地地址；`/api` 已代理至 8000 端口。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q
npm.cmd --prefix frontend test
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
- `data/warehouse/csi300-2020/`：官方主源沪深300历史仓库、逐股标准行情、历史成分、预热日历、校验清单及自动处理报告。
- `runs/factors/{id}/result.json`：一次固定评分批次，含原始值、处理分数、日期、权重及来源。
- 历史动态批次在同目录另存`history.parquet`/`diagnostics.parquet`，`result.json`保存校验索引；因子评估的完整标签也单独存Parquet，网页提供下载。
- `runs/backtests/{id}/result.json`：Qlib 结果、持仓、成交、原生报告及运行配置。
- `runs/evaluations/{id}/result.json` 与 `report.md`：逐日 IC、分组收益、标签明细、相关性与可独立阅读的报告。
- `runs/experiments/{id}/`：冻结计划、验证期选择记录、各段子任务引用、校验和及比较报告。
- `runs/tasks/catalog.sqlite3`：网页研究配置、排队与执行状态、错误和具体结果引用。
- 样本 ID 包含运行指纹；同版本重跑复用已有结果，代码或依赖改变后产生不同 ID。历史查询绑定评分批次，不混合版本。
- 默认路径相对于运行目录，应从项目根目录启动；CLI 可用 `--runs-dir` 和 `--data-dir` 指定位置。API 工厂也可显式指定结果目录。
- 每次样本数据导出在 `data/sample-qlib/{运行指纹}/` 下保留独立特征文件；不会覆盖其他版本的快照。
- 密钥不应写入源码、运行产物、前端或日志；Tushare适配器读取后端本地配置。

合成样本基准是合成价格均值；真实回测可沿用原价格基准或独立选择官方全收益基准。真实策略按Qlib复权收益/等效持股口径计算，现金分红不另记现金；全收益基准计入分红再投资，但不代表已复刻红利税、到账日与真实再投资账本。开盘触及买入涨停价或卖出跌停价时保守不成交。独立核验确认的整日停牌按此前已知收盘价估值并禁止买卖，缺失PE仅沿用此前可得值；未知缺数仍拒绝发布。尚未支持退市清算、盘中停牌和逐笔分红账本。每次运行详情会保留这些假设。本地历史仓库已下载；没有实盘操作。

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

第三方接入首轮状态见 [真实数据接入记录](docs/real-data-status.md)。后续为财报版本验收、历史股票池、正式数据库、旧因子迁移和网页历史分段实验配置，详见 [架构规划](docs/architecture-plan.md)。

已补充财报公告/修订的时点函数与反例测试，真实财报接入尚未完成；已发现并修正的时间边界问题见 [专项检查](docs/lookahead-audit.md)。
