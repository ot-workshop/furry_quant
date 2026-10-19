# 第一版前后端契约

本期为个人本地运行的研究结果查看器，使用明确标注的合成样本，不使用真实 API 密钥。后端使用 Qlib 真正计算样本回测，不伪造回测结果。前端 React + TypeScript + ECharts，项目放 frontend/。

Vite 开发端口 5173，/api 代理到 http://127.0.0.1:8000。构建后由同一个 FastAPI 后端托管。

GET /api/v1/overview
返回 {"mode":"synthetic", "notice":"合成样本，仅验证系统流程，不代表真实股票表现", "factor_runs":1,"backtests":1,"instruments":3}

GET /api/v1/instruments?q=
返回 {"items":[{"instrument":"SH600000","name":"样本甲","exchange":"SH"},...],"total":3}

GET /api/v1/factor-runs
返回 {"items":[{"id":"demo-factors","name":"样本多因子评分","as_of":"2025-01-17","universe":"合成样本股票池","sample_size":3,"scheme":"demo-v1","data_version":"synthetic-v1","is_synthetic":true}],"total":1}

GET /api/v1/factor-runs/{id}/scores?instrument=&date=2025-01-17&sort_by=composite&descending=true&offset=0&limit=50
返回 {"items":[{"instrument":"SH600000","name":"样本甲","industry":"样本金融","date":"2025-01-17","raw":{"momentum":0.02,"value":0.1,"quality":0.15},"scores":{"momentum":100.0,"value":66.67,"quality":33.33},"composite":66.67,"rank":1}],"total":3,"offset":0,"limit":50,"metadata":{...评分批次属性...,"weights":{"momentum":0.4,"value":0.3,"quality":0.3},"score_unit":"percentile_0_100","direction":"高分表示更符合样本因子的偏好，不代表上涨概率"}}

GET /api/v1/instruments/{id}/factor-history?scheme=demo-v1&run_id=demo-factors
返回 {"items":[{"date":"2025-01-06","momentum":66.67,"value":33.33,"quality":100.0,"composite":66.67,"run_id":"demo-factors"},...],"scheme":"demo-v1","is_synthetic":true}

GET /api/v1/backtests
返回 {"items":[{"id":"demo-qlib","name":"Qlib 样本轮换策略","status":"succeeded","strategy":"TopkDropoutStrategy","start_date":"2025-01-07","end_date":"2025-01-17","created_at":"ISO timestamp","is_synthetic":true,"metrics":{"total_return":0.02,"max_drawdown":-0.01,"total_cost":0.001}}],"total":1}

GET /api/v1/backtests/{id}
返回同上属性，并额外包含 "engine":"qlib", "engine_version":"0.9.7", "data_version":"synthetic-v1", "config":{...}, "limitations":["合成样本，不可用于投资判断",...], "metrics":{...数值...}

GET /api/v1/backtests/{id}/equity
返回 {"items":[{"date":"2025-01-06","equity":1.0,"benchmark":1.0,"drawdown":0.0,"cost":0.0},...]}

GET /api/v1/backtests/{id}/positions?date=&offset=0&limit=50
返回 {"items":[{"date":"2025-01-07","instrument":"SH600000","name":"样本甲","amount":100.0,"price":10.0,"weight":0.1,"cash":5000.0}],"total":1,"offset":0,"limit":50}

GET /api/v1/backtests/{id}/trades?offset=0&limit=50
返回 {"items":[{"date":"2025-01-07","instrument":"SH600000","side":"buy","amount":100.0,"price":10.0,"value":1000.0,"cost":5.0}],"total":1,"offset":0,"limit":50}

GET /api/v1/backtests/{id}/download
返回可下载的结果 JSON。

404 返回 {"detail":"..."}。结果尚未生成时列表为空；详情不存在时返回404。前端要有加载、空状态和错误提示，不能静默替换成自己生成的假数据。

页面：股票因子（搜索股票、批次日期、原始值/百分位得分、历史线）、因子排名（排序、点击查看股票）、回测结果（列表、净值/基准、回撤、费用、持仓/成交表、配置与限制、下载）。同口径排名来自后端，前端只展示。评分与回测标识是独立的，不能把最新评分冒充历史回测决策时的评分。第一版只读，不提供看似可用却没有后端的任务按钮。

实现补充：实际 demo ID 含代码/依赖/Python 指纹后缀，应始终从列表读取，不硬编码上方示例 ID。评分批次列表包含 available_dates；scores 的 date 默认为批次最新日期。多批次共享 scheme 时，历史查询未指定 run_id 返回 409，避免静默混合。费用指标 total_cost 为费用/初始本金，total_cost_amount 为金额；avg_turnover 为日均换手率。组合分是加权百分位分，不是组合本身的排名百分位。
