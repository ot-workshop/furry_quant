# Tushare 第一批接入契约

范围：用户授权的第三方 Tushare SDK 兼容服务。真实请求由主 agent 执行，协作 agent 不读取或接收密钥。

实现位置仅 src/finance_quant/data/providers/ 与 tests/test_tushare_provider.py。

## 公共接口

`TushareSettings.from_env(env_file: Path | None = None)`：读取 TUSHARE_TOKEN、TUSHARE_HTTP_URL，可用 python-dotenv（已安装）读取本地 .env，进程环境优先。token 不在 repr 中显示。URL 默认 https://api.tushare.pro；第三方配置要求 https、无 userinfo/query/fragment。无 token 报易懂的配置错误，不回显值。

`TushareProvider(settings, min_interval=0.35, client=None)`：lazy import tushare；ts.pro_api(settings.token, timeout=20)，仅在该适配器内设置 _DataApi__http_url。允许注入模拟 client 供离线测试。

`provider.query(api_name: str, *, fields: str = "", **params) -> pd.DataFrame`：官方主源使用标准HTTPS JSON请求直达`https://api.tushare.pro`根路径，禁止重定向；避免本机SDK中的代理路径/额外参数影响官方调用。显式配置的第三方兼容服务保留SDK调用方式。限制最小调用间隔；不记录token；不在异常中回显参数/响应正文。公开异常类型TushareProviderError。空结果是空DataFrame，由上层决定是否可接受。网络错误最多重试2次，权限/参数错误不重试。接口名严格标识校验。

可定义 `normalize_daily(raw)`、`normalize_adj_factor(raw)`、`normalize_calendar(raw)`、`normalize_index_daily(raw)`、`normalize_daily_basic(raw)`。

标准行情结果：instrument（600000.SH -> SH600000）、date（无时区日期Timestamp）、open/high/low/close/pre_close、volume（Tushare手数乘100为股）、amount（千元乘1000为元），change（pct_chg除100）。OHLC必须有限正数，volume允许0由后续判定停牌，重复instrument/date拒绝。缺列、非法代码/日期、非数值价格必须明确报错。不要用forward fill。

复权：instrument/date/adj_factor（原始累计因子，不直接当Qlib $factor）；日历：date/is_open（布尔）；每日指标：instrument/date，加pe_ttm/pb/total_mv字段，总市值由万元乘10000转为元，缺失估值保留NaN，不填0；指数行情与股票日线同价格字段，成交量由手转股、金额由千元转元。

离线测试至少覆盖：注入client调用参数、错误不泄露token、无token/不安全URL、单位、日期、代码、重复、价格异常、空响应。不要测试外网，不更改任何其他文件，不安装依赖，不读取 .env、data/ 或用户配置。
