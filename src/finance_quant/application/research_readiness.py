"""Calendar-only readiness checks; no provider calls or research computation."""

import pandas as pd


def validate_download_calendar(config, calendar):
    sessions=pd.DatetimeIndex(calendar.loc[calendar.is_open,'date'])
    start,end=pd.Timestamp(config.backtest.start),pd.Timestamp(config.backtest.end)
    if start not in sessions or end not in sessions:
        raise ValueError('回测起止日期必须是实际交易日，请先计算日期调整方案')
    validate_warmup(config.data.start,config.data.end,config.backtest.start,config.factors,calendar)
    if int(((sessions>=start)&(sessions<=end)).sum())<=max(config.evaluation.horizons):
        raise ValueError('回测交易日数须超过最长评估持有期')


def cached_calendar(registry, data_start, backtest_start):
    start, until = pd.Timestamp(data_start), pd.Timestamp(backtest_start)
    candidates = [(pd.Timestamp(manifest['end']), calendar)
                  for _, _, calendar, manifest in registry.values()
                  if pd.Timestamp(manifest['start']) <= start and pd.Timestamp(manifest['end']) >= until]
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def validate_warmup(data_start, data_end, backtest_start, factors, calendar=None):
    required = max(spec.lookback for spec in factors.specs) + 1
    start, end, execution = map(pd.Timestamp, (data_start, data_end, backtest_start))
    if calendar is None:
        # A calendar day is an upper bound, never an invented trading session.
        upper = max(0, (execution - start).days)
        if upper < required:
            raise ValueError(f'历史预热不足：当前因子至少需要{required}个回测前历史交易日；'
                             f'数据起点至回测起点最多只有{upper}个自然日，无法满足。'
                             '请提前数据开始日期或延后回测起点')
        return
    sessions = pd.DatetimeIndex(calendar.loc[calendar.is_open & calendar.date.between(start, end), 'date'])
    if execution not in sessions:
        raise ValueError('回测开始日期必须是实际交易日')
    available = int((sessions < execution).sum())
    if available < required:
        earliest = sessions[required].strftime('%Y-%m-%d') if len(sessions) > required else None
        suggestion = (f'按已缓存交易日历，最早回测从{earliest}开始。' if earliest else '当前数据区间不足以完成预热。')
        raise ValueError(f'历史预热不足：当前因子至少需要{required}个回测前历史交易日，实际只有{available}个。'
                         + suggestion + '如需保留原回测起点，请提前数据开始日期')
