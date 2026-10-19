import hashlib
import json
import pandas as pd
import pytest

from finance_quant.data.quality import adjustment_discontinuities
from finance_quant.data.managed import download_dataset, DatasetDownloadError
from finance_quant.data.market import load_market
from finance_quant.configuration import DataConfig
from test_managed_data import Provider


def test_adjustment_audit_accepts_real_split_and_flags_mixed_bases():
    market = pd.DataFrame({'instrument':['SH600000']*3,'date':pd.to_datetime(['2025-01-02','2025-01-03','2025-01-06']),
                           'close':[10.,5.,5.], 'adj_factor':[1.,2.,2.], 'change':[0.,0.,0.]})
    assert adjustment_discontinuities(market) == []
    market.loc[1,'adj_factor'] = .02
    errors = adjustment_discontinuities(market)
    assert [row['date'] for row in errors] == ['2025-01-03','2025-01-06']


def test_large_legitimate_daily_move_is_not_misclassified_as_market_rule():
    market = pd.DataFrame({'instrument':['BJ430047']*2,'date':pd.to_datetime(['2025-01-02','2025-01-03']),
                           'close':[10.,13.], 'adj_factor':[1.,1.], 'change':[0.,.3]})
    assert adjustment_discontinuities(market) == []
    market.loc[1,'adj_factor'] = 1.15
    assert adjustment_discontinuities(market)


class MixedFactorProvider(Provider):
    def query(self, api, **params):
        frame = super().query(api, **params)
        if api == 'adj_factor':
            frame.loc[frame.trade_date == '20250108', 'adj_factor'] = .01
        return frame


def test_complete_dates_with_wrong_adjustment_never_publish(tmp_path):
    config = DataConfig(tmp_path/'data','20241202','20250228',('600000.SH','000001.SZ'))
    with pytest.raises(DatasetDownloadError,match='quality checks'):
        download_dataset(config,provider=MixedFactorProvider())
    assert not (config.root/'manifest.json').exists()
    from finance_quant.data.catalog import catalog_status
    report = catalog_status(config.root)['jobs'][0]['report']
    assert report['status'] == 'incomplete' and report['adjustment_anomalies']


def test_loader_independently_rejects_checksum_valid_but_misadjusted_prices(tmp_path):
    config = DataConfig(tmp_path/'data','20241202','20250228',('600000.SH','000001.SZ'))
    manifest = download_dataset(config,provider=Provider())['manifest']
    snapshot = config.root/manifest['snapshot_path']
    file = snapshot/'market.parquet'; market = pd.read_parquet(file)
    market.loc[(market.instrument=='SH600000')&(market.date==pd.Timestamp('2025-01-08')),'adj_factor'] = .01
    market.to_parquet(file,index=False)
    leaf=json.loads((snapshot/'manifest.json').read_text(encoding='utf-8'))
    leaf['files']['market.parquet']=hashlib.sha256(file.read_bytes()).hexdigest()
    (snapshot/'manifest.json').write_text(json.dumps(leaf),encoding='utf-8')
    with pytest.raises(ValueError,match='continuity'):
        load_market(config.root)
