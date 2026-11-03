"""Safe download failure summaries from structured local catalog evidence."""

from __future__ import annotations

import re
from pathlib import Path

from finance_quant.data.catalog import catalog_status

LABELS = {'daily': '日线', 'daily_basic': '日估值', 'adj_factor': '复权因子', 'stk_limit': '涨跌停',
          'trade_cal': '交易日历', 'index_daily': '基准行情', 'stock_basic': '股票资料', 'secondary_history': 'BaoStock 独立核验', 'market': '行情质量'}
CATEGORIES = {'missing_sessions': '缺少交易日记录', 'invalid_response': '空响应或字段不完整',
              'stock_metadata_unavailable': '股票资料为空或不受服务支持',
              'outside_listing_period': '所选区间包含上市前或退市后日期，请调整数据起止日期',
              'extra_sessions': '含交易日历之外的记录', 'duplicate_sessions': '同一交易日记录重复',
              'invalid_market_values': '行情、复权或涨跌停价格无效',
              'network_failed': '网络请求失败', 'access_denied': '服务拒绝访问', 'rate_limit': '触发服务限流',
              'unsupported': '接口不受支持', 'cache_corrupt': '缓存校验失败',
              'secondary_unavailable': '第二数据源不可用', 'raw_price_conflict': '两源原始价格不一致',
              'secondary_connection_failed': 'BaoStock 连接或查询失败，重试后仍不可用，请稍后重试',
              'secondary_invalid': 'BaoStock 记录缺失或复权校验未通过',
              'plausible_adjusted_return_conflict': '两源复权收益不一致',
              'primary_failed_verify_only': '主源异常且当前模式不允许回退',
              'secondary_unavailable_invalid_or_conflicting': '第二数据源缺失、异常或冲突'}


def describe_download_failure(root: Path, *, since: str | None = None, job_id: str | None = None) -> dict | None:
    status = catalog_status(root)
    jobs = status.get('jobs', [])
    if job_id:
        jobs = [job for job in jobs if job['id'] == job_id]
    if not jobs or jobs[0]['status'] in {'running', 'succeeded'} or (since and jobs[0]['started_at'] < since):
        return None
    job = jobs[0]
    report = job.get('report') or {}
    issues = []
    for failure in report.get('failures', []):
        params = failure.get('params', {})
        api = failure.get('api')
        if api not in LABELS:
            continue
        dates = failure.get('missing_dates') or ([params['trade_date']] if params.get('trade_date') else [])
        dates = sorted({date if '-' in date else f'{date[:4]}-{date[4:6]}-{date[6:]}' for date in dates
                        if isinstance(date, str) and re.fullmatch(r'\d{8}|\d{4}-\d{2}-\d{2}', date)})
        code = params.get('ts_code', '')
        if code and not re.fullmatch(r'\d{6}\.(SH|SZ|BJ)', code):
            continue
        category = failure.get('error', 'request_failed')
        if category not in CATEGORIES:
            category = 'request_failed'
        issues.append({'code': code, 'api': api, 'category': category, 'dates': dates[:31],
                       'description': f"{LABELS[api]}：{CATEGORIES.get(category, '请求失败')}"})
    for section in [*report.get('stocks', []), report.get('benchmark', {})]:
        code = section.get('code', '')
        if not re.fullmatch(r'\d{6}\.(SH|SZ|BJ)', code):
            continue
        for field, category in [('missing_dates', 'missing_sessions'), ('extra_dates', 'extra_sessions'),
                                ('duplicate_dates', 'duplicate_sessions'), ('invalid_dates', 'invalid_market_values')]:
            dates = [date for date in section.get(field, []) if isinstance(date, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', date)]
            if dates:
                issues.append({'code': code, 'api': 'market', 'category': category, 'dates': dates[:31],
                               'description': f'行情质量：{CATEGORIES[category]}'})
    for stock in report.get('cross_validation', {}).get('stocks', []):
        if stock.get('status') == 'conflict':
            dates = [r['date'] if isinstance(r, dict) else r for r in stock.get('raw_price_differences', []) or stock.get('return_differences', [])]
            category = stock.get('reason', 'secondary_unavailable')
            if category not in CATEGORIES:
                category = 'secondary_unavailable'
            issues.append({'code': stock['code'], 'api': 'secondary_history', 'category': category, 'dates': dates[:31],
                           'description': f"独立核验：{CATEGORIES[category]}"})
    # Primary daily/factor holes are expected inputs to whole-series fallback.
    # A failed secondary check or an unrecoverable field is the actual blocker.
    fallback = job.get('config', {}).get('validation', {}).get('mode') == 'fallback'
    recoverable = {'request_failed', 'network_failed', 'missing_sessions', 'invalid_response'}
    def priority(row):
        deferred = fallback and row['api'] in {'daily', 'adj_factor'} and row['category'] in recoverable
        return (deferred, row['api'] != 'secondary_history', row['category'] != 'missing_sessions', -len(row['dates']))
    issues.sort(key=priority)
    if not issues:
        return None
    first = issues[0]
    date_note = f"，{first['dates'][0]}" + (f" 等{len(first['dates'])}个交易日" if len(first['dates']) > 1 else '') if first['dates'] else ''
    summary = f"{first['code']} {LABELS[first['api']]}：{CATEGORIES.get(first['category'], '下载或校验失败')}{date_note}。未发布这次数据，已完成分片会保留。"
    if fallback and first['api'] == 'secondary_history':
        summary += '主源行情或复权缺失需用 BaoStock 整段核验后回退；独立核验未通过，不能填充复权因子。'
    return {'job_id': job['id'], 'status': job['status'], 'summary': summary, 'issues': issues[:20]}
