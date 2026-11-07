import json

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from finance_quant.data.industries import POLICY, IndustryHistory, load_industries, publish_industries
from finance_quant.research.market_factors import market_factors
from finance_quant.research.neutralization import NeutralizationConfig, factor_industry_diagnostics, residualize
from finance_quant.research.registry import FactorConfig, FactorSpec
from finance_quant.storage import ResultStore


def history(symbols, *, switch=False):
    rows = []
    for i, s in enumerate(symbols):
        group = i // 2
        rows.append(
            dict(
                instrument=s,
                industry_code=f"I{group}",
                industry_name=f"行业{group}",
                classification="SW2021",
                in_date=pd.Timestamp("2010-01-01"),
                out_date=pd.Timestamp("2025-02-03") if switch and i == 0 else pd.NaT,
            )
        )
        if switch and i == 0:
            rows.append(
                {
                    **rows[-1],
                    "industry_code": "I1",
                    "industry_name": "行业1",
                    "in_date": pd.Timestamp("2025-02-03"),
                    "out_date": pd.NaT,
                }
            )
    return IndustryHistory(
        pd.DataFrame(rows), {"id": "a" * 64, "source": "fixture", "policy": POLICY, "point_in_time_certified": False}
    )


def test_asof_change_out_date_and_taxonomy_are_causal():
    h = history(["SH600000", "SZ000001"], switch=True)
    old = {
        **h.frame.iloc[0].to_dict(),
        "classification": "SW2014",
        "industry_code": "OLD",
        "industry_name": "旧行业",
        "out_date": pd.NaT,
    }
    h = IndustryHistory(pd.concat([h.frame, pd.DataFrame([old])]), h.manifest)
    cal = pd.bdate_range("2020-01-01", "2025-03-01")
    assert h.at("2020-01-02", ["SH600000"], cal)["SH600000"]["industry_code"] == "OLD"
    assert h.at("2021-12-10", ["SH600000"], cal)["SH600000"]["industry_code"] == "OLD"
    assert h.at("2021-12-13", ["SH600000"], cal)["SH600000"]["industry_code"] == "I0"
    assert h.at("2025-02-03", ["SH600000"], cal)["SH600000"]["industry_code"] == "I0"
    assert h.at("2025-02-04", ["SH600000"], cal)["SH600000"]["industry_code"] == "I1"
    # Current backcast label must not leak before the SW2021 classification starts.
    assert history(["SZ000001"]).at("2020-01-02", ["SZ000001"], cal)["SZ000001"]["industry_status"] == "missing"


def test_overlap_missing_and_same_industry_duplicates():
    h = history(["SH600000", "SZ000001"])
    f = h.frame
    h = IndustryHistory(pd.concat([f, f.iloc[[0]], f.iloc[[1]].assign(industry_code="CONFLICT")]), h.manifest)
    found = h.at("2025-01-02", ["SH600000", "SZ000001", "SH600999"], pd.bdate_range("2025-01-01", periods=3))
    assert [found[s]["industry_status"] for s in found] == ["known", "conflict", "missing"]


def test_no_taxonomy_before_release_or_labels_beyond_cache_asof():
    h = history(["SH600000"])
    h.frame["classification"] = "SW2014"
    h.frame["in_date"] = pd.Timestamp("1990-01-01")
    h.manifest["data_asof"] = "2025-02-03"
    assert (
        h.at("2010-01-04", ["SH600000"], pd.bdate_range("2010-01-01", periods=5))["SH600000"]["industry_status"]
        == "missing"
    )
    with pytest.raises(ValueError, match="仅核验至"):
        h.validate_range("2025-02-04")
    assert (
        h.at("2025-02-04", ["SH600000"], pd.bdate_range("2025-02-01", periods=5))["SH600000"]["industry_status"]
        == "missing"
    )


def test_residuals_match_independent_dummy_regression():
    v = pd.Series([1.0, 4.0, 18.0, 19.0, 25.0, 10000.0])
    g = pd.Series(["a", "a", "b", "b", "c", "c"])
    y = np.clip(v, *np.quantile(v, [0.01, 0.99]))
    x = np.column_stack([g.eq(k) for k in sorted(g.unique())]).astype(float)
    residual = y - x @ np.linalg.lstsq(x, y, rcond=None)[0]
    expected = residual / np.std(residual)
    np.testing.assert_allclose(residualize(v, g), expected, atol=1e-12)
    np.testing.assert_allclose(x.T @ residualize(v, g), 0, atol=1e-12)


def test_on_weights_windows_and_future_industry_changes_reach_real_scores():
    from test_factor_library import frame

    market = frame(8, 70)
    h = history(sorted(market.instrument.unique()))
    mode = NeutralizationConfig("industry")
    first = FactorConfig((FactorSpec("value", 0.6), FactorSpec("momentum", 0.4, 5)))
    different = FactorConfig((FactorSpec("value", 0.1), FactorSpec("momentum", 0.9, 25)))
    a = market_factors(market, "fixture", first, industry_history=h, neutralization=mode)
    b = market_factors(market, "fixture", different, industry_history=h, neutralization=mode)
    day = a["metadata"]["as_of"]
    left = [r for r in a["history"] if r["date"] == day]
    right = [r for r in b["history"] if r["date"] == day]
    assert [r["scores"] for r in left] != [r["scores"] for r in right]
    assert [r["composite"] for r in left] != [r["composite"] for r in right]
    for r in right:
        assert r["composite"] == pytest.approx(0.1 * r["scores"]["value"] + 0.9 * r["scores"]["momentum"])
    # A new future industry is deliberately different; it cannot relabel early signals.
    f = h.frame.copy()
    f.loc[0, "out_date"] = pd.Timestamp("2025-01-02")
    new = {
        **f.iloc[0].to_dict(),
        "in_date": pd.Timestamp("2025-01-02"),
        "out_date": pd.NaT,
        "industry_code": "FUTURE",
        "industry_name": "未来行业",
    }
    future = IndustryHistory(pd.concat([f, pd.DataFrame([new])]), h.manifest)
    assert (
        market_factors(market, "fixture", first, industry_history=future, neutralization=mode)["history"]
        == a["history"]
    )


def test_download_history_serial_resume_and_refuse_truncation(tmp_path):
    from finance_quant.data.industries import download_industries

    class Provider:
        calls = []

        def query(self, api, **params):
            self.calls.append((api, params))
            if api == "index_classify":
                return pd.DataFrame([{"index_code": "801780.SI", "industry_name": "银行"}])
            field = "con_code" if api == "index_member" else "ts_code"
            code_field = "index_code" if api == "index_member" else "l1_code"
            return pd.DataFrame(
                [{field: "000001.SZ", code_field: "801780.SI", "in_date": "19900101", "out_date": None}]
            )

    p = Provider()
    h = download_industries(root=tmp_path / "cache", provider=p)
    assert len(p.calls) == 6 and set(h.frame.classification) == {"SW2014", "SW2021"}
    assert download_industries(root=tmp_path / "cache", provider=p).manifest["id"] == h.manifest["id"]
    assert len(p.calls) == 6
    cached = tmp_path / "cache/raw/SW2021-L1-801780.SI-Y.parquet"
    f = pd.read_parquet(cached)
    pd.concat([f] * 2000).to_parquet(cached, index=False)
    with pytest.raises(ValueError, match="截断"):
        download_industries(root=tmp_path / "cache", provider=p)


@pytest.mark.parametrize("size", [4, 8, 15])
def test_switch_effect_and_off_reproduction_with_future_change(size):
    from test_factor_library import frame

    market = frame(size, 60)
    symbols = sorted(market.instrument.unique())
    h = history(symbols, switch=True)
    config = FactorConfig((FactorSpec("value", 0.6), FactorSpec("momentum", 0.4, 10)))
    baseline = market_factors(market, "test", config)
    off = market_factors(market, "test", config, industry_history=h, neutralization=NeutralizationConfig("off"))
    on = market_factors(market, "test", config, industry_history=h, neutralization=NeutralizationConfig("industry"))
    original_fields = ["raw", "scores", "contributions", "composite", "rank", "date", "instrument"]
    assert [{k: r[k] for k in original_fields} for r in baseline["history"]] == [
        {k: r[k] for k in original_fields} for r in off["history"]
    ]
    assert any(a["composite"] != b["composite"] for a, b in zip(off["history"], on["history"]))
    # Direction aligned values and scores are recomputed independently each day.
    day = on["metadata"]["as_of"]
    rows = [r for r in on["history"] if r["date"] == day]
    df = pd.DataFrame(rows)
    groups = df.industry_code
    for key in config.weights:
        values = pd.Series([r["raw"][key] for r in rows])
        y = np.clip(values, *np.quantile(values, [0.01, 0.99]))
        x = pd.get_dummies(groups, dtype=float).to_numpy()
        res = y - x @ np.linalg.lstsq(x, y, rcond=None)[0]
        res = res / np.sqrt(np.mean(res**2))
        expected = pd.Series(res).rank(pct=True) * 100
        np.testing.assert_allclose([r["processed"][key] for r in rows], res, atol=1e-10)
        np.testing.assert_allclose([r["scores"][key] for r in rows], expected)
    future = h.frame.copy()
    future.loc[future.instrument.eq(symbols[-1]), "out_date"] = pd.Timestamp("2026-01-02")
    again = market_factors(
        market,
        "test",
        config,
        industry_history=IndustryHistory(future, h.manifest),
        neutralization=NeutralizationConfig("industry"),
    )
    assert on["history"] == again["history"]
    report = factor_industry_diagnostics(rows, on["metadata"])
    for row in report["items"]:
        assert max(abs(v) for v in row["residual_mean"].values()) < 1e-10


def test_singleton_only_pool_fails_clearly_and_missing_is_not_zero():
    from test_factor_configuration import sample_market

    m = sample_market()
    h = history(sorted(m.instrument.unique()))
    h.frame["industry_code"] = ["a", "b", "c"]
    with pytest.raises(ValueError, match="行业中性化后无有效区分"):
        market_factors(
            m,
            "fixture",
            FactorConfig((FactorSpec("value", 1.0),)),
            industry_history=h,
            neutralization=NeutralizationConfig("industry"),
        )


def test_versioned_cache_and_config_roundtrip(tmp_path):
    from finance_quant.configuration import BacktestConfig, DataConfig, ResearchConfig, load_config

    h = publish_industries(history(["SH600000", "SZ000001"]).frame)
    assert load_industries(h.manifest["id"]).manifest == h.manifest
    c = ResearchConfig(
        DataConfig(tmp_path / "data", "2024-01-01", "2025-02-28", ("600000.SH", "000001.SZ")),
        BacktestConfig("2025-01-02", "2025-02-28", topk=1),
        tmp_path / "runs",
        neutralization=NeutralizationConfig("industry", h.manifest["id"]),
    )
    p = tmp_path / "config.json"
    p.write_text(json.dumps(c.as_dict()), encoding="utf-8")
    assert load_config(p) == c
    path = tmp_path / "industries" / h.manifest["id"] / "intervals.parquet"
    path.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="校验失败"):
        load_industries(h.manifest["id"])


@pytest.mark.parametrize("size,seed", [(4, 23), (8, 109)])
def test_full_queue_native_and_off_rerun(tmp_path, size, seed):
    from test_pool_workflow_matrix import codes_for, request_for, run_request

    codes = codes_for(size, seed)
    symbols = [c[-2:] + c[:6] for c in codes]
    document = request_for(codes)
    baseline = run_request(document, tmp_path / "data", tmp_path / "runs")
    assert baseline["status"] == "succeeded", baseline["error"]
    publish_industries(history(symbols).frame)
    offdoc = {**document, "neutralization": {"mode": "off"}}
    off = run_request(offdoc, tmp_path / "data", tmp_path / "runs")
    ondoc = {**document, "neutralization": {"mode": "industry"}}
    on = run_request(ondoc, tmp_path / "data", tmp_path / "runs")
    assert off["status"] == on["status"] == "succeeded", (off["error"], on["error"])
    store = ResultStore(tmp_path / "runs")
    read = lambda task: store.get("backtests", task["result"]["backtest_run_id"])
    before, after, neutral = map(read, [baseline, off, on])
    for key in ["trades", "positions", "equity"]:
        assert before[key] == after[key]
    assert neutral["trades"] != after["trades"]
    assert neutral["metadata"]["factor_run_id"] == on["result"]["factor_run_id"]
    assert neutral["industry_exposure"]["days"]
    # Real API reads this task's frozen industry diagnostics, never latest cache.
    from support.fakes import make_app

    with TestClient(make_app(tmp_path / "runs", data_root=tmp_path / "data")) as client:
        factor_id = on["result"]["factor_run_id"]
        run_id = on["result"]["backtest_run_id"]
        report = client.get(f"/api/v1/factor-runs/{factor_id}/industries").json()
        assert report["neutralization"]["mode"] == "industry" and report["items"]
        exposure = client.get(f"/api/v1/backtests/{run_id}/industries").json()
        assert exposure["available"] and exposure["items"]
        assert sum(r["weight"] for r in exposure["items"]) + exposure["cash_weight"] == pytest.approx(1)
        assert client.get(f"/api/v1/backtests/{run_id}/industries?date=1900-01-01").status_code == 422
