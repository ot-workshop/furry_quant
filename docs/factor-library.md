# 日频因子库

因子公式、参数、方向、输入字段和缺失规则集中登记在 `src/finance_quant/research/factor_library.py`。配置校验、计算、网页选项和结果明细读取同一份定义；Qlib 负责组合执行和 IC 计算。

## 指标口径

`P` 为按各股票首日归一的累计复权收盘价，`r` 为复权日收益。窗口按交易所交易日推进，停牌不压缩日历。波动率按 252 个交易日年化，标准差使用样本标准差。

| 名称 / 配置名 | 默认参数 | 原始值 | 评分方向 |
| --- | --- | --- | --- |
| 动量 `momentum` | `window=20, skip=0` | `P[t-skip] / P[t-window] - 1` | 高 |
| 盈利收益率 `value` | 无窗口，`missing_policy=zero` | 信号日可得的 TTM 归母净利润 / 当日总市值 | 高 |
| 波动率 `volatility` | `window=10` | 窗口收益标准差 × `√252` | 低 |
| 账面价值收益率 `book_to_price` | 无窗口 | `1 / 正PB` | 高 |
| 小市值 `size` | 无窗口 | `-ln(总市值/CNY)` | 高 |
| 短期反转 `reversal` | `window=21, skip=1` | `-(P[t-skip] / P[t-window] - 1)` | 高 |
| 中期动量 `medium_momentum` | `window=126, skip=21` | `P[t-skip] / P[t-window] - 1` | 高 |
| 下行波动率 `downside_volatility` | `window=20` | 负收益标准差 × `√252` | 低 |
| 滚动最大回撤 `max_drawdown` | `window=60` | `min(P / cummax(P) - 1)` | 高 |
| 成交额异动 `amount_anomaly` | `window=20, baseline_window=60` | 近期均额 / 此前非重叠基准均额 | 高 |

动量窗口是总跨度。`126/21` 使用 `t-126` 至 `t-21` 的收益。下行波动率至少需要两个负收益观察。成交额异动要求两个窗口各有至少 80% 的正成交额日期，且当日有成交。

`window` 和 `baseline_window` 接受 2–252 的整数，`skip` 接受 0 至 `window-1`。盈利收益率、PB、市值不接受窗口。

盈利收益率保留负数和零：亏损公司以负 E/P 参与排名，零利润的原始值为 0。利润与市值统一使用元，不从空 PE 推断是否亏损。TTM 只使用信号日15:00已经可用的报告，市值遵循当日估值的可用时间；信号供下一交易日开盘使用。财报和市值的完整历史修订时点仍未认证。

在因子行选择缺失处理方式：`zero`（默认）对真正缺失记0分，`neutral` 记50分，`exclude` 剔除股票。前两项填补的是百分位评分，原始值仍为空，其他因子权重不放大。0分会惩罚数据缺失，50分表示中性；两种方式均可能改变行业暴露。没有任何真实因子值的股票不参与排名，停牌、预热与股票池约束仍适用。

“因子评分 → 个股明细”可以查看 TTM 归母净利润、当日市值、报告期、报告可用时间与实际填补情况。单因子 IC、分组收益和因子相关性只使用真实值；综合分的诊断包含所选填补规则。缺失处理、财务版本和计算版本进入结果标识，旧结果按原始值与原公式保留，不自动重算。

## 基本面因子

`roe_ttm`、`roa_ttm`、`gross_margin_ttm`、`asset_turnover_ttm`、`debt_to_assets`、`accruals_ttm`、`profit_growth_ttm`、`revenue_growth_ttm` 来自三张财务报表，口径为 TTM 或时点存量，详见 [financial-data.md](financial-data.md)。

- 不接收窗口参数，方向与公式见因子定义。
- 除允许缺失评分的盈利收益率、毛利率外，需要研究数据集绑定财务数据版本；其他财务因子缺少数据时不可选。
- 毛利率采用TTM营业收入与营业成本的匹配口径；银行、保险、证券、多元金融报表不适用，固定记50分。真正缺失默认记0分，网页可切换为记50分或剔除；原始值仍为空。
- 毛利率不适用状态按当时可用报表类型，或固定版本的信号日历史行业判断，不用当前行业回填。个股计算明细展示类型、收入、成本及TTM组成报告；填补值不进入单因子IC及相关性。
- 行业差异大，建议与行业中性化一起使用；中性化可选申万一级/二级/三级行业（默认一级）。

## 配置

每个因子实例拥有独立参数、方向和权重。同一因子可以配置多个不同窗口，但实例 `id` 必须唯一：

```json
{
  "factors": [
    {"name": "momentum", "id": "fast", "weight": 0.3, "window": 20},
    {"name": "momentum", "id": "slow", "weight": 0.3, "window": 60, "skip": 5},
    {"name": "value", "weight": 0.2, "missing_policy": "zero"},
    {"name": "amount_anomaly", "weight": 0.2, "window": 20, "baseline_window": 60}
  ]
}
```

启用实例的权重必须为正且总和为 1。网页按百分数输入，JSON 使用小数。同一研究最多启用 20 个实例。

## 横截面评分

1. 计算每个因子的原始值。
2. 要求严格处理的因子有效，且股票至少有一个真实因子值；盈利收益率和毛利率允许按配置保留真正缺失项，毛利率行业不适用项单独处理。
3. 按配置方向计算平均秩百分位。
4. 盈利收益率和毛利率真正缺失时，按配置填补评分或剔除，毛利率行业不适用固定记50分；开启行业中性化时先对真实值处理，再赋分。
5. 百分位乘权重得到分项贡献，贡献之和为综合分。

至少两只股票符合条件才发布评分。原始缺失值保留为空，不自动删除因子或重新分配权重。结果保存全股票池、可评分数量、各因子的真实有效/缺失/填补数量、原始值和排除原因。除盈利收益率外的因子继续采用严格缺失规则。

## 本地手动公式

在仓库根目录执行 `uv sync --locked` 后，可以从 JSON 登记固定股票池的价格收益率。公式只接受下面两个字段，`window` 为 2–252 的整数。计算使用复权收盘价 `P[t] / P[t-window] - 1`，沿用动量的交易日日历、缺失剔除及排序。回测使用信号日后下一交易日的开盘价。

```json
{"operator": "price_return", "window": 2}
```

```sh
uv run --locked python -m finance_quant.manual_formula --runs-dir ./runs register ./formula.json
uv run --locked python -m finance_quant.manual_formula --runs-dir ./runs run --data-root ./storage/data --config ./manual-research.json --formula-id FORMULA_ID
uv run --locked python -m finance_quant.manual_formula --runs-dir ./runs replay --data-root ./storage/data --evaluation-id EVALUATION_ID --output ./replayed-v1
```

把前一个命令输出的 ID 填入下一步。配置示例中的日期和股票须与所选行情一致，并为信号日留出窗口和下一交易日。

```json
{
  "data": {
    "root": "./storage/data/research/downloads/three-stock-batch",
    "start": "2025-01-02",
    "end": "2025-01-10",
    "codes": ["600000.SH", "000001.SZ", "600519.SH"]
  },
  "backtest": {"start": "2025-01-08", "end": "2025-01-10", "topk": 1},
  "evaluation": {"horizons": [1], "groups": 3, "min_cross_section": 3}
}
```

`data.root` 相对配置文件解析，可以指向已发布批次或其具体行情叶。命令把公式设为唯一因子，权重为 1。数据使用受管固定池、随行情叶保存的基准和交易日历。适用范围为本地离线研究，含回测和上述区间的因子评估。

内容相同的公式返回同一个完整 SHA-256 标识；调整窗口后登记新版本。原评估在来源中保存规范公式、运算版本、行情叶相对路径、清单及三个文件的摘要、股票池与运行参数。重放从指定评估恢复原设置，校验原公式记录、计算代码、依赖锁、Python 版本及冻结文件，然后在新目录实际重算评分、回测与评估。保留原运行目录、所选行情叶以及生成结果时的仓库版本和锁文件，便于以后重放。失败退出码为 1，原运行目录的 `formula_failures` 下有失败阶段和错误码。

### 本地滚动验证

先登记两个不同窗口的公式，然后依次固定计划、运行和重放。把命令中的 `ROLLING_ID` 替换为固定计划输出的 ID。

```sh
uv run --locked python -m finance_quant.manual_formula --runs-dir ./runs rolling-plan --data-root ./storage/data --plan ./rolling.json
uv run --locked python -m finance_quant.manual_formula --runs-dir ./runs rolling-run --data-root ./storage/data --rolling-id ROLLING_ID
uv run --locked python -m finance_quant.manual_formula --runs-dir ./runs rolling-replay --data-root ./storage/data --rolling-id ROLLING_ID --output ./replayed-rolling
```

`rolling.json` 包含以下字段。

- `research` 使用上节的研究配置。回测起止日期覆盖两折测试的实际入场日，交易参数由两折共用。评估固定为一日持有、三组及每天至少三股，省略 `evaluation` 即采用该设置。
- `formula_ids` 是两个已登记的完整公式 ID。
- `folds` 是两个对象，每个对象含 `train`、`validation`、`test` 三个字段。每个字段写成 `["起始信号日", "结束信号日"]`，两端均纳入。使用冻结交易日历的日期，分别保持两折对应区段等长，并按时间排列两折互不重叠的测试段。
- `report_cutoff` 是带时区偏移的 ISO 8601 时间，供测试标签判断是否可得，例如 `"2025-03-07T18:00:00+08:00"`。
- `evidence` 指向数据根目录下的可得时刻 JSON。该文件包含 `schema_version: 1`、行情清单的 `data_version` 和 `market_sha256`（`market.parquet` 的 SHA-256）、`certification`（`synthetic` 或 `unverified`）及 `rows`。每行记录 `date`、`instrument`（如 `SH600000`）、`open_available_at` 和 `adjustment_available_at`。后两个字段使用带时区偏移的时刻，未知时填 `null`。该证据仅表示计划采用的可得时间，结果保留未认证状态。

信号日 T 对应 T+1 复权开盘入场、T+2 复权开盘退出。标签在退出日北京时间 15:00 和两端行情、复权证据均已可得时成熟。训练和验证以后一段之前一交易日 18:00 截止，依次排除退出落入后段、退出落在紧邻后段的交易日，以及截止时尚不可得的标签。已有回测链对成交所需行情执行原有校验。

训练段记录覆盖和缺失。验证段在两候选都能评分的共同股日上比较 Rank IC，两候选都有效的每日至少有三只股票，且至少有两日。以每日 Rank IC 均值选赢家，同分按公式 ID 升序。每折先将逐股筛选证据和选择封存在 `selection.json`，再仅测试赢家。样本不足的折记录原因。

结果在 `runs/experiments/ROLLING_ID/`，包含 `plan.json`、`selection.json` 和 `result.json`，测试工件引用指向评分、回测及使用报告截止时点的评估。保留原行情叶、证据文件、公式仓库、计划和运行环境。重放在新目录复算，核对封存选择及各折结果后发布滚动结果。

## 时间边界

- 因子只读取信号日及此前数据。
- 收盘后可用的 T 日信号最早用于下一交易日开盘。
- 未来收益只用于因子评估，不进入评分。
- 输入日历缺失或重复时拒绝计算，不把后续行移动到错误日期。
- 估值字段保存观察日期和可用时间；显式晚于信号时点的值拒绝使用。
- 供应商 PE、PB 和市值的完整历史修订时刻无法证明时，结果不会标记为严格 PIT 认证。

数据版本、股票池、因子实例、参数、权重和因子代码共同决定评分结果标识。修改资金、交易费率或回测区间可以复用评分；修改股票池或因子配置会生成新评分。

## 扩展因子

新增因子需要同时提供：

- 因子库定义和参数约束。
- 纯历史输入的计算器。
- 缺失和异常值规则。
- 最小预热跨度。
- 时间边界与未来数据扰动测试。
- 网页显示名称、公式和字段说明。

网页不提供任意 Python 代码执行入口。
