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
| `/evaluations`、`/evaluations/<id>` | 因子评估 | IC 汇总、分组收益、相关性、覆盖、方法 |
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

`npm test` 运行 `frontend/tests/`：表单规则的纯函数测试，以及在 jsdom 中挂载应用、模拟接口的交互测试。
