# 前端结构与接口

前端位于 `frontend/`（React 19 + TypeScript + Ant Design 6 + ECharts）。开发时 Vite 运行在 5173 端口并把 `/api` 代理到 `127.0.0.1:8000`；构建产物由 FastAPI 托管。

## 页面与路由

| 路由 | 页面 | 说明 |
| --- | --- | --- |
| `/` | 工作台 | 数据集、进行中任务、最近回测 |
| `/research/new?from=<task>&market=<dataset>` | 新建研究 | 固定池、沪深300历史成分及动态条件池；右侧摘要与校验；`from`载入历史任务，`market`选择对应覆盖版本 |
| `/tasks?focus=<task>` | 任务中心 | 研究与方案比较任务，3 秒自动刷新 |
| `/backtests`、`/backtests/<id>/<tab>` | 回测 | 列表 → 详情（概览、股票池筛选、持仓、成交、费用与调仓、行业暴露、执行记录、参数与口径） |
| `/factors?run=&date=&instrument=&tab=` | 因子评分 | 横截面排名、个股详情、行业、批次信息；选择状态都在 URL 中 |
| `/evaluations`、`/evaluations/<id>` | 因子评估 | IC 汇总、滚动时序、相关性热图、分组收益、覆盖、方法 |
| `/experiments`、`/experiments/new`、`/experiments/<id>` | 方案比较 | 列表、新建、详情 |
| `/data?tab=` | 数据管理 | 统一更新、备份与恢复、研究数据集、行情下载（含沪深300历史成分）、财务数据、行业数据、数据库 |

所有页面状态（选中的回测、标签页、评分日期、股票）都体现在 URL 中，可直接分享或刷新。

## 设计约定

- 颜色只来自 `src/app/theme.tsx`：AntD 组件用设计令牌，自定义样式用 AntD CSS 变量，图表用 `useChartPalette()`；涨跌色由用户的“红涨绿跌 / 绿涨红跌”设置决定。因此深色模式不需要逐处覆盖。
- 只使用 AntD 控件（不使用原生 `select`/`input type=date`）。
- 加载、错误、空状态统一使用 `components/ui.tsx` 的 `Resource`、`EmptyState`、`ErrorState`。
- 口径说明与已知限制默认折叠在“口径与限制”中，不铺满页面。
- 后台刷新保留已有数据（`useApiResource` 的 stale-while-revalidate），不会闪烁清空。

## 主要接口

结果（只读）：

- `GET /api/v1/overview`、`GET /api/v1/data-status`（研究数据集与数据库概况）
- `GET /api/v1/factor-runs`、`/factor-runs/{id}/scores`、`/factor-runs/{id}/industries`、`/factor-runs/{id}/tables/{name}`
- `GET /api/v1/instruments?run_id=`、`/instruments/{code}/factor-history?scheme=&run_id=`
- `GET /api/v1/backtests`、`/backtests/{id}`、`/equity`、`/positions`、`/trades`、`/industries`、`/execution-events`、`/download`
- `GET /api/v1/evaluations`、`/evaluations/{id}`、`/labels/{h}`、`/report`、`/download`
- `GET /api/v1/experiments`、`/experiments/{id}`、`/report`、`/download`

任务与数据（POST 需本机来源、JSON 请求体；提交任务需 `Idempotency-Key`）：

- `GET /api/v1/research/options`、`GET /api/v1/research/tasks`、`GET /api/v1/research/tasks/{id}`
- `POST /api/v1/research/tasks`（研究）、`POST /api/v1/research/experiments`（方案比较）、`POST /api/v1/research/tasks/{id}/cancel`
- `GET /api/v1/research/market-download`、`POST .../preview`、`POST .../start`、`POST .../pause`
- `GET /api/v1/research/financial-download`、`POST .../preview`、`POST .../start`、`POST .../pause`
- `GET /api/v1/research/industry-download`、`POST .../preview`、`POST .../start`、`POST .../pause`
- `GET /api/v1/research/data-updates`、`POST .../preview`、`POST .../start`、`POST .../pause`、`POST .../schedule`
- `GET /api/v1/research/backups`、`POST /api/v1/research/backups`（创建、校验、恢复至独立空环境）

错误统一返回 `{"detail": "..."}`，前端原样展示。结果不存在时返回 404，前端不会用自造数据替代。

## 测试

在仓库根目录执行 `npm --prefix frontend test`，或进入 `frontend/` 执行 `npm test`。它运行表单规则的纯函数测试，以及在 jsdom 中挂载应用、模拟接口的交互测试；不替代真实浏览器、worker 和 PostgreSQL 的端到端验收。完整步骤与清理要求见[测试指南](testing.md)。

## 财务状态与旧结果兼容

`GET /api/v1/evaluations/{id}` 与 `/evaluations/{id}/download` 在响应中按冻结定义和绑定证据校正 `metadata.temporal_audit.items` 的 `financials` 项；不写回存档，不修改 `factors`、IC、分组收益或运行 ID。评估列表、Markdown 报告、分析、选优和标签读取不应用此投影。

`components/audit.tsx` 的标签为：`not_used` → 未使用、`missing_binding` → 未绑定、`unknown` → 无法判断、`unverified` → 未认证。财务依赖来自冻结入选因子的 `requires_financials` 定义，不能用“允许缺失评分后能否启动”替代。只在请求中出现的版本不是实际使用证据；旧字段空值或格式异常不能被解释成有效版本。面向用户的完整说明见[财务数据](financial-data.md#研究绑定)。


## 滚动诊断接口

`GET /api/v1/evaluations/{id}/analysis` 从已保存的评估记录计算，只读且兼容已有结果。

| 参数 | 说明 |
| --- | --- |
| `horizon` | 已保存的收益持有期，必填 |
| `factor` | 因子实例键，默认 `composite` |
| `window` | 回看信号交易日数，2–2520，默认 60 |
| `min_periods` | 最少有效日期，2 至 window，默认等于 window |
| `start` / `end` | 展示与相关性汇总的信号日期范围；滚动计算保留范围之前的记录 |
| `first` / `second` | 查看历史相关性的因子对 |

`series` 提供每日 IC/Rank IC、滚动均值、未年化 IR、有效数与 `available_on`。后者为窗口内已使用收益标签的最晚退出日，不能把这些统计作为信号日已知信息。缺失交易日保留在窗口内，标准差为零和样本不足返回 null。相关性 `cells` 提供平均相关、平均绝对相关及有效日期数；不对缺失单元格填零。

### 因子证据体检

`GET /api/v1/evaluations/{id}/evidence` 从已保存评估产物及关联冻结因子批次只读计算，不修改结果文件或计算指纹。

| 参数 | 说明 |
| --- | --- |
| `horizon` | 已保存的收益持有期（1–252），必填 |
| `factor` | 因子实例键，默认 `composite` |
| `start` / `end` | 可选信号日期筛选范围（`YYYY-MM-DD`） |
| `cutoff` | 成熟截止日（`YYYY-MM-DD`）；评估结果缺少 `metadata.end` 时必填 |
| `lag` | Bartlett 滞后阶数（0–251），默认 `horizon - 1` |

返回状态（`ready` / `no_valid_observations` / `insufficient_observations` / `undefined_variance` / `missing_frozen_calendar` / `no_signal_dates`）、`counts`（含 `paired_observations` 逐日最小/最大与全区间合计）、`rank_ic`（仅 `ready` 时给出 `se`/`ci_low`/`ci_high`/`variance`）、`label_coverage`（标明为信号区间内按原评估截止日统计，部分日或原因明细不全时抑制排除小计）、`raw_availability`（按字段及预期组成因子逐日独立判断是否覆盖筛选区间全部信号日，含 `matched_signal_dates`/`total_signal_dates` 及综合分 `component_imputations`）、`common_sample`（仅在政策为 `signal_date_common_v1` 且携带 12/64 位十六进制指纹时标记已冻结共同样本，其余策略或空对象保持未知）及 `financials`（映射校正后的 `temporal_audit` 财务绑定状态）。缺失冻结日历、未保存字段或部分日期匹配均保留 `null` / 未知，不回填为 0。


### 因子组合筛选

- `GET /api/v1/evaluations/{id}/selection`：参数为 `horizon`、`cutoff`、`factor_count`、`min_ic`、`min_ir`、`max_correlation`、`min_dates` 与可选 `start`。返回预筛阶段统计、候选原因、组合、不可行时的最大数量和不可变来源引用。
- `GET /api/v1/evaluations/{id}/selection/draft`：上述参数加 `selection_id`，返回可编辑的分段比较草稿和来源版本。没有足够后续行情返回 422。
- 实验请求可携带 `factor_selection`，仅接受 `evaluation_id`、`selection_id` 和筛选参数。服务端重新核验来源、窗口、等权权重、股票池、行业设置和所有比较日期；不信任客户端传来的推荐结果。
- 这两个 GET 不发布结果、不下载数据。任务执行前再次核对来源摘要，完成后在实验元数据和报告中保存筛选证据。


### 收益来源对照

使用相同的 `POST /api/v1/research/experiments` 队列入口。`plan.comparison` 包含 `baseline_id` 和 `sample_mode`（`common` 或 `independent`），此时 `plan.periods` 只有 `analysis: {start, end}`。各候选额外接受 `neutralization`；所有方案仍共用行情、财务、行业版本、股票池规则和交易参数。普通三段比较的请求与结果结构不变，不能把MILP选优来源混入历史收益对照。

结果仍位于 `/api/v1/experiments/{id}`，元数据 `validation_kind=return_comparison`，`selection=null`。`rows` 按 `candidate_id/sample` 记录各次独立回测，`comparisons` 保存独立样本收益差、共同样本收益差和两者差额，`sample_days` 保存逐日基础池、各候选可评分数及共同样本数。共同样本完整股票可通过相应 `factor_run_id` 的评分接口按日查询。共同样本的来源摘要进入回测与结果标识；旧文件不覆写。报告与JSON导出继续使用实验结果的既有下载入口。

### 因子覆盖查询

`gross_margin_ttm` 的 `missing_policy=zero|neutral|exclude` 仅控制真正缺失，行业不适用固定赋50分。覆盖查询可传已选 `industry_version`，不传时不会借用后续新增的历史行业。响应分别返回 `available`（原始有效）、`not_applicable` 和 `unavailable`（真实缺失或交易/历史限制），三者之和为 `total`。不适用本身不触发黄色缺失图标。评分覆盖额外保存 `not_applicable/not_applicable_scored`；个股 `factor_details` 保存 `status`、`reason_label`、`company_type`、`revenue_ttm`、`operating_cost_ttm` 和 `components`（组成报告期、系数、类型、收入成本和可用时间）。不适用及缺失的原始值均为 null，依靠状态区分；其填补值不进入单因子评估。

`POST /api/v1/research/factor-coverage` 为只读核验。请求包含股票池 `data`、回测首日 `start`、单因子 `factor`（名称及窗口参数，不传权重），可选 `financial_version` 固定财务版本。返回首个信号日、数据版本、独立可评分/不可评分数量、互斥主因统计、少量股票示例及口径说明。结果不代表全区间覆盖，也不计入其他因子或行业中性化的进一步限制。该接口不创建任务、下载或发布数据。

盈利收益率 `factor.name=value` 接受 `missing_policy=zero|neutral|exclude`，默认 `zero`，研究、方案比较和覆盖查询使用同一字段。返回 `breakdown` 中的正数、零、负数数量；`available/unavailable` 表示真实值及交易条件的覆盖，不把填补评分计入真实有效数。评分响应的 `coverage.factors.<key>` 保存 `positive/zero/negative/imputed/missing_policy`；每只股票的 `factor_details.<key>` 保存TTM利润、市值、报告期、可用时间和 `imputed`。原始缺失值返回 null，零利润返回数值0。
