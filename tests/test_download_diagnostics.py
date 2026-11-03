import json

import pytest

from finance_quant.application.download_diagnostics import describe_download_failure
from finance_quant.application.tasks import TaskStore, TaskConflict
from finance_quant.data.catalog import Catalog


def test_missing_valuation_dates_are_reported_without_mislabeling_permission(tmp_path):
    root = tmp_path / 'data'
    catalog = Catalog(root)
    catalog.begin_job('download-first', {})
    catalog.finish('download-first', 'partial', {'failures': [
        {'api': 'daily_basic', 'params': {'ts_code': '600150.SH', 'trade_date': '20250813'}, 'error': 'invalid_response'},
        {'api': 'daily_basic', 'params': {'ts_code': '600150.SH'}, 'error': 'missing_sessions', 'missing_dates': ['2025-08-13', '2025-08-14', '2025-08-15', '2025-08-18']}
    ]})
    details = describe_download_failure(root)
    assert '600150.SH' in details['summary'] and '4个交易日' in details['summary']
    assert '权限' not in details['summary']
    assert details['issues'][0]['dates'][-1] == '2025-08-18'
    catalog.begin_job('download-later', {})
    assert describe_download_failure(root) is None
    assert describe_download_failure(root, job_id='download-first') == details
    assert describe_download_failure(root, job_id='download-first', since='9999') is None
    catalog.close()


def test_failure_diagnostics_never_include_unknown_provider_text(tmp_path):
    catalog = Catalog(tmp_path)
    catalog.begin_job('download-secret', {})
    catalog.finish('download-secret', 'failed', {'failures': [
        {'api': 'daily', 'params': {'ts_code': '600150.SH'}, 'error': 'token=do-not-expose'}]})
    assert 'do-not-expose' not in json.dumps(describe_download_failure(tmp_path))
    catalog.close()


def test_failure_annotation_keeps_request_and_finish_time_unchanged(tmp_path):
    store = TaskStore(tmp_path)
    store.initialize()
    task = store.create({'name': 'test', 'data': {'mode': 'download'}}, {'mode': 'download'}, 'diagnostic-test-123')
    store.claim()
    store.finish(task['id'], 'failed', error='old generic error')
    failed = store.get(task['id'])
    details = {'job_id': 'download-first', 'summary': '600150.SH 日估值缺失', 'issues': []}
    updated = store.annotate_failure(task['id'], details)
    assert updated['request'] == task['request']
    assert updated['finished_at'] == failed['finished_at'] and updated['status'] == 'failed'
    assert updated['error_details'] == details
    second = store.create({'name': 'queued', 'data': {'mode': 'download'}}, {}, 'diagnostic-second-123')
    with pytest.raises(TaskConflict):
        store.annotate_failure(second['id'], details)


def test_quality_gate_failure_reports_the_actual_stock_and_bad_dates(tmp_path):
    catalog = Catalog(tmp_path)
    catalog.begin_job('download-quality', {})
    catalog.finish('download-quality', 'partial', {'stocks': [
        {'code': '300750.SZ', 'invalid_dates': ['2025-05-06'], 'missing_dates': [], 'duplicate_dates': [], 'extra_dates': []}
    ]})
    details = describe_download_failure(tmp_path)
    assert details['issues'][0]['category'] == 'invalid_market_values'
    assert '300750.SZ' in details['summary'] and '2025-05-06' in details['summary']
    assert '权限' not in details['summary']
    catalog.close()


def test_secondary_blocker_is_not_hidden_by_many_recoverable_factor_holes(tmp_path):
    catalog = Catalog(tmp_path)
    catalog.begin_job('download-fallback', {'validation': {'mode': 'fallback'}})
    holes = [{'api': 'adj_factor', 'params': {'ts_code': '000776.SZ'},
              'error': 'missing_sessions', 'missing_dates': ['2023-03-01']} for _ in range(170)]
    fatal = {'api': 'secondary_history', 'params': {'ts_code': '600030.SH'}, 'error': 'secondary_unavailable'}
    catalog.finish('download-fallback', 'failed', {'failures': [*holes, fatal]})
    details = describe_download_failure(tmp_path)
    assert details['issues'][0]['code'] == '600030.SH'
    assert details['issues'][0]['api'] == 'secondary_history'
    assert '600030.SH BaoStock 独立核验' in details['summary']
    assert '不能填充复权因子' in details['summary']
    catalog.close()
