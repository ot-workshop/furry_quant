# 数据管理与可配置回测

已增加可选`data.validation.mode`（off/verify/fallback）和BaoStock联合核验。当前推荐`configs/research.joint.json`；已有按日缓存也会先复用，主源异常时按整只股票区间切换，未解决冲突不发布。命令、边界和实测见[联合数据核验](joint-data-validation.md)。

本轮将固定三股示例扩展为配置驱动的数据获取与研究流程。配置文件不含密钥，数据更新与离线回测分开执行；网页新增“数据管理”只读页面。

## 配置与使用

`configs/research.verified.json` 为本轮通过检查的五股2026年窗口；`configs/research.example.json`保留三股短示例。`configs/research.two-year.json`为多年诊断配置，因供应商复权尺度异常而未通过，不作为可用回测案例。所有配置均为固定股票池，不是历史指数成分。

```powershell
.\.venv\Scripts\python.exe -m finance_quant.cli plan --config configs\research.verified.json
.\.venv\Scripts\python.exe -m finance_quant.cli download --config configs\research.verified.json
.\.venv\Scripts\python.exe -m finance_quant.cli status --config configs\research.verified.json
.\.venv\Scripts\python.exe -m finance_quant.cli real --config configs\research.verified.json
.\start.ps1 -Config configs\research.verified.json
```

`plan`、`status`、`real` 和启动服务不联网；只有 `download` 和 `probe` 访问供应商。`plan` 给出没有缓存时的常规请求数量上限，不包括缺失记录的按日补抓及网络重试。

配置包含数据目录、日期、股票池、价格指数基准，以及回测日期、本金、持股数、每日替换数量、仓位比例和示例费率。路径相对于配置文件解析。代码和日期会标准化，重复代码、非法区间、未知字段、重复JSON字段和非有限数值会被拒绝。使用 `--config` 时不允许混用同类命令行覆盖项，避免实际参数不明确。

回测时配置的股票池、数据区间和基准必须与已发布快照一致；仅改配置不会静默改用另一批行情。数据应包含足够的因子预热期，回测首日前一交易日必须有评分，后续决策日也不能缺评分。回测参数写入运行指纹，修改本金、仓位或费用等会产生独立结果；相同版本重跑复用旧结果。

成功执行带配置的回测后，会在结果目录保存 `active-research.json` 配置副本。默认 `start.ps1` 使用 `runs/active-research.json`；也可用 `-Config` 指定原配置文件。默认只启动网页，未建立激活配置时也不自动计算或下载，可在网页配置任务；`-RunResearch` 显式先计算研究，`-Demo` 显式生成合成样本，`-Demo` 与 `-Config`/`-RunResearch` 互斥。脚本保持UTF-8 BOM，以兼容Windows PowerShell 5.1。见[启动说明](startup.md)。

## 分片、重试与质量检查

- 请求按供应商来源、接口、代码和年度日期范围标识。已经完成的分片验证原始文件摘要后复用；扩展日期或股票池只获取未缓存的请求，边界年份区间变化需重新请求；交易日历按月缓存。
- 按年度边界批量拉取行情，逐月检查覆盖；最多三个HTTP请求并行，所有请求仍共用供应商适配器的启动间隔限制。SQLite、校验、发布均在调用线程执行。网络和频率错误有有限重试；兼容服务未分类的请求异常额外重试一次，权限和不支持错误不重复尝试。
- 每个分片独立记录成功、失败、尝试次数、抓取时间、行数和摘要。失败后重跑会保留成功分片。损坏缓存先报错，下次重跑重新获取该请求并保留原证据文件。
- 真实验收发现：该兼容服务部分月度复权因子返回漏行，但按具体交易日可返回记录。因此逐月检查后，每个月缺失不超过31个交易日时，使用实际按日请求补抓；请求与原始响应均保留。不会插值、前向填充复权因子或补造行情。仍不完整或日期范围异常时终止。
- 交易日历必须完整；股票和指数按官方日历检查缺失、多余、重复日期及价格、成交量、复权、涨跌停字段。缺失日期不直接推断为停牌。
- 缺失或非正PE保留为缺失提示，不填0，不参与估值因子；行情快照可以保留这些股票，因子覆盖另行记录。
- 明确错误后停止后续批量请求，保留当前进度。进程被强制结束时，下一次取得下载锁后将旧运行标记为中断。
- 同一数据目录只允许一个下载任务。已有原始请求默认不自动刷新；需要重取历史修订时执行相同命令并加 `--refresh`。刷新使用独立缓存代次，失败时普通下载仍使用上一成功代次；带 `--refresh` 重跑会续传同一待完成代次，成功后才切换，避免混用新旧刷新计划。

## 存储与发布

`data/managed/catalog.sqlite3` 只保存下载元数据、任务与快照索引；标准行情仍是Parquet文件。这不是已经完成PostgreSQL市场数据库。

原始返回文件和脱敏请求元数据保存在 `raw/`，覆盖报告保存在 `reports/`。新标准文件先在临时目录完成，并通过日期、字段及复权连续性检查，再发布到 `snapshots/{version}/`，最后更新根目录 `manifest.json`。校验失败不推进当前快照；已有快照不覆盖。显式刷新会生成新的来源记录，旧快照和运行继续可读。

网页通过只读 `/api/v1/data-status` 查询配置数据目录中的快照和最近30次任务。页面可查看覆盖日期、股票数量、数据行数、逐股缺口、估值提示、失败请求和缓存复用数量。查询不会联网、建库或触发下载；覆盖信息是下载验收记录，不代替日后文件审计。

## 当前边界

尚未建设网页任务提交、后台调度、PostgreSQL市场数据库、历史指数成分、停牌/退市处理及财报历史修订。固定股票池的多年回测不能据此声称消除了幸存者偏差。费用、分红和成交约束沿用既有验收口径，详见 `real-data-status.md`。

接口字段参考：[日线](https://tushare.pro/document/2?doc_id=27)、[复权因子](https://tushare.pro/document/2?doc_id=28)、[每日指标](https://tushare.pro/document/2?doc_id=32)。第三方服务实际行为以保存的请求和响应为准。

Qlib派生特征存放于结果目录 `.qlib/{fingerprint}/`，不写入不可变行情快照；相同工作目录的回测使用文件锁，忙时明确报错。不同回测通过独立CLI进程运行，不在API请求内共享Qlib全局状态。

复权异常会显示在数据管理的失败任务详情中，包括标的、相邻日期、复权收益和接口涨跌幅。发布与加载均独立检查，超过当前保守阈值需核实公司行动或供应商数据，不自动缩放修复。多年数据失败证据见[供应商问题](provider-data-issues.md)；当前有效结果见[验收记录](real-data-status.md)。启动脚本可用`-Port`指定本机端口。

配置现支持可选 `factors` 与 `evaluation`，用于本地因子选择、窗口、权重以及IC/分组诊断；不改变数据下载规则。网页任务、CLI计算命令或 `start.ps1 -RunResearch` 会生成或复用回测与评估，默认启动只打开网页。详细字段和报告口径见[因子研究](factor-research.md)。
