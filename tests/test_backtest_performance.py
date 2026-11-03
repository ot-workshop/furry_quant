import copy
import json
import math

import pytest
from fastapi.testclient import TestClient

from finance_quant.api import create_app
from finance_quant.research.performance import calculate_performance, enrich_backtest
from finance_quant.storage import ResultStore


def curve(values=(1., 1.1, .99, 1.0395)):
    return [{'date': f'2025-01-0{index + 1}', 'equity': value, 'benchmark': 2 + index * .02,
             'drawdown': 0., 'cost': .05} for index, value in enumerate(values)]


def legacy_result():
    return {'summary': {'id': 'old-result', 'name': '旧回测', 'status': 'succeeded', 'is_synthetic': False,
                        'strategy': 'Qlib', 'start_date': '2025-01-02', 'end_date': '2025-01-04', 'created_at': '2025-01-05',
                        'metrics': {'total_return': .0395, 'total_cost': .001, 'avg_turnover': .25}},
            'metadata': {'engine': 'qlib'}, 'equity': curve(), 'trades': [{'cost': 5.}], 'positions': []}


def test_hand_calculated_after_cost_statistics_and_initial_day_count():
    result = calculate_performance(curve())
    metrics = result['metrics']
    expected_std = math.sqrt(((.1 - 1 / 60) ** 2 + (-.1 - 1 / 60) ** 2 + (.05 - 1 / 60) ** 2) / 2)
    annual = 1.0395 ** 84 - 1  # Three returns, not four NAV observations.
    assert metrics['total_days'] == 3 and metrics['years'] == 3 / 252
    assert metrics['total_return'] == pytest.approx(.0395)
    assert metrics['annualized_return'] == pytest.approx(annual)
    assert metrics['volatility'] == pytest.approx(expected_std * math.sqrt(252))
    assert metrics['sharpe_ratio'] == pytest.approx((252 / 60 - .03) / (expected_std * math.sqrt(252)))
    assert metrics['max_drawdown'] == pytest.approx(-.1)
    assert metrics['calmar_ratio'] == pytest.approx((annual - .03) / .1)
    assert metrics['win_rate'] == 2 / 3 and metrics['profit_loss_ratio'] == pytest.approx(.75)
    assert result['drawdown'] == {'peak_date': '2025-01-02', 'trough_date': '2025-01-03'}
    assert result['methodology']['annual_risk_free_rate'] == .03
    # Gross returns or a second fee deduction would give different numbers.
    assert result['benchmark_metrics']['total_return'] == pytest.approx(.03)


def test_annual_bars_compound_each_return_in_its_own_year_including_first_day():
    dates = ['2024-12-27', '2024-12-30', '2024-12-31', '2025-01-02', '2025-01-03']
    values = [1., .9, .99, .891, .98]
    result = calculate_performance([{'date': day, 'equity': v, 'benchmark': v * 4} for day, v in zip(dates, values)])
    first, second = result['annual_returns']
    assert first['year'] == 2024 and second['year'] == 2025
    assert first['start_date'] == '2024-12-30' and second['start_date'] == '2025-01-02'
    assert first['trading_days'] == second['trading_days'] == 2
    assert first['strategy_return'] == pytest.approx(-.01)
    assert second['strategy_return'] == pytest.approx(.98 / .99 - 1)
    assert (1 + first['strategy_return']) * (1 + second['strategy_return']) - 1 == pytest.approx(-.02)
    assert result['benchmark_metrics'] == result['metrics']


def test_flat_no_trade_path_preserves_zero_but_undefined_ratios_are_null():
    result = enrich_backtest({**legacy_result(), 'equity': curve((1., 1., 1., 1.)), 'trades': []})
    metrics = result['summary']['metrics']
    assert metrics['total_return'] == metrics['volatility'] == metrics['max_drawdown'] == 0
    assert metrics['trade_count'] == 0
    for field in ['sharpe_ratio', 'calmar_ratio', 'win_rate', 'profit_loss_ratio']:
        assert metrics[field] is None
    assert result['performance']['drawdown']['trough_date'] is None
    json.dumps(result, allow_nan=False)


def test_one_return_has_no_sample_volatility_or_sharpe():
    metrics = calculate_performance(curve((1., .999)))['metrics']
    assert metrics['total_return'] == pytest.approx(-.001)
    assert metrics['total_days'] == 1
    assert metrics['volatility'] is None and metrics['sharpe_ratio'] is None


@pytest.mark.parametrize('values,win_rate', [((1., 1.1, 1.1, 1.2), 1.), ((1., .9, .9, .8), 0.)])
def test_daily_win_rate_excludes_flat_days_and_one_sided_profit_loss_is_undefined(values, win_rate):
    metrics = calculate_performance(curve(values))['metrics']
    assert metrics['win_rate'] == win_rate
    assert metrics['profit_loss_ratio'] is None


@pytest.mark.parametrize('invalid', [None, float('nan'), float('inf'), -1., True])
def test_invalid_nav_is_not_dropped_or_filled(invalid):
    points = curve()
    points[1]['equity'] = invalid
    result = calculate_performance(points)
    assert not result['available']
    assert all(value is None for value in result['metrics'].values())
    assert result['annual_returns'] == []
    json.dumps(result, allow_nan=False)


def test_incomplete_benchmark_does_not_invalidate_strategy_or_get_filled():
    points = curve()
    points[2]['benchmark'] = None
    result = calculate_performance(points)
    assert result['available'] and result['metrics']['total_return'] == pytest.approx(.0395)
    assert all(v is None for v in result['benchmark_metrics'].values())
    assert result['annual_returns'][0]['benchmark_return'] is None


@pytest.mark.parametrize('mutation', ['duplicate', 'unordered', 'bad-date'])
def test_bad_date_alignment_fails_without_manufacturing_observations(mutation):
    points = curve()
    if mutation == 'duplicate': points[1]['date'] = points[0]['date']
    elif mutation == 'unordered': points.reverse()
    else: points[1]['date'] = 'not-a-date'
    with pytest.raises(ValueError):
        calculate_performance(points)
    enriched = enrich_backtest({**legacy_result(), 'equity': points})
    assert not enriched['performance']['available']
    assert enriched['summary']['metrics']['sharpe_ratio'] is None


def test_legacy_api_list_detail_download_agree_without_modifying_artifact(tmp_path):
    store = ResultStore(tmp_path)
    original = legacy_result()
    path = store.publish('backtests', 'old-result', original)
    before = path.read_bytes()
    client = TestClient(create_app(tmp_path))
    listing = client.get('/api/v1/backtests').json()['items'][0]
    detail = client.get('/api/v1/backtests/old-result').json()
    download = client.get('/api/v1/backtests/old-result/download').json()
    assert listing['metrics'] == detail['metrics'] == download['summary']['metrics']
    assert detail['performance'] == download['performance']
    assert detail['metrics']['volatility'] > 0 and detail['metrics']['sharpe_ratio'] is not None
    assert detail['metrics']['avg_turnover'] == .25
    assert client.get('/api/v1/backtests/old-result/equity').json()['items'] == original['equity']
    assert path.read_bytes() == before and store.get('backtests', 'old-result') == original


def test_enrichment_is_pure_and_only_uses_the_selected_run():
    first, second = legacy_result(), legacy_result()
    second['equity'] = curve((1., .95, .9, .85))
    original = copy.deepcopy(first)
    first_view, second_view = enrich_backtest(first), enrich_backtest(second)
    assert first == original and 'performance' not in first
    assert first_view['summary']['metrics']['total_return'] > 0 > second_view['summary']['metrics']['total_return']
    assert enrich_backtest(first_view) == first_view
    failed = {**legacy_result(), 'summary': {'status': 'failed'}}
    assert enrich_backtest(failed) == failed and 'performance' not in failed
