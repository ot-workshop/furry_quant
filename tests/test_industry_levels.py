"""Level-aware SW industry storage, download and neutralization."""

import pandas as pd
import pytest
from test_factor_library import frame

from finance_quant.data.industries import (
    IndustryDownloadIncomplete,
    IndustryHistory,
    download_industries,
    load_industries,
    normalize_sectors,
    optional_industries,
    publish_industries,
)
from finance_quant.data.network_budget import DownloadPaused
from finance_quant.research.market_factors import market_factors
from finance_quant.research.neutralization import (
    NeutralizationConfig,
    industry_context,
    parse_neutralization,
)
from finance_quant.research.registry import FactorConfig, FactorSpec


def catalogs():
    return {
        ("SW2014", "L1"): [
            {
                "index_code": "801020.SI",
                "industry_name": "采掘",
                "level": "L1",
                "industry_code": "210000",
                "parent_code": "0",
            }
        ],
        ("SW2014", "L2"): [
            {
                "index_code": "801023.SI",
                "industry_name": "石油开采",
                "level": "L2",
                "industry_code": "210100",
                "parent_code": "210000",
            }
        ],
        ("SW2014", "L3"): [
            {
                "index_code": "850211.SI",
                "industry_name": "石油开采",
                "level": "L3",
                "industry_code": "210101",
                "parent_code": "210100",
            }
        ],
        ("SW2021", "L1"): [
            {
                "index_code": "801010.SI",
                "industry_name": "农林牧渔",
                "level": "L1",
                "industry_code": "110000",
                "parent_code": "0",
            }
        ],
        ("SW2021", "L2"): [
            {
                "index_code": "801016.SI",
                "industry_name": "种植业",
                "level": "L2",
                "industry_code": "110100",
                "parent_code": "110000",
            }
        ],
        ("SW2021", "L3"): [
            {
                "index_code": "850111.SI",
                "industry_name": "种子",
                "level": "L3",
                "industry_code": "110101",
                "parent_code": "110100",
            }
        ],
    }


class Provider:
    """A deterministic provider that returns one stock per sector and records every call."""

    def __init__(self, fail=None, pause=None):
        self.calls = []
        self.fail = fail
        self.pause = pause

    def query(self, api, **params):
        self.calls.append((api, params))
        if self.pause and self.pause(api, params):
            (self.pause_file).write_text("pause", encoding="utf-8")
        if api == "index_classify":
            level = params["level"]
            taxonomy = params.get("src", "SW2014")
            return pd.DataFrame(catalogs()[(taxonomy, level)])
        code = params.get("index_code") or params.get("l1_code") or params.get("l2_code") or params.get("l3_code")
        if self.fail and self.fail(api, params):
            raise RuntimeError("vendor failed")
        if api == "index_member":
            return pd.DataFrame(
                [
                    {
                        "index_code": code,
                        "con_code": "000001.SZ",
                        "in_date": "19900101",
                        "out_date": None,
                        "is_new": params.get("is_new"),
                    }
                ]
            )
        return pd.DataFrame(
            [
                {
                    "l1_code": params.get("l1_code", "801010.SI"),
                    "l1_name": "农林牧渔",
                    "l2_code": params.get("l2_code", "801016.SI"),
                    "l2_name": "种植业",
                    "l3_code": params.get("l3_code", "850111.SI"),
                    "l3_name": "种子",
                    "ts_code": "000001.SZ",
                    "name": "平安银行",
                    "in_date": "19900101",
                    "out_date": None,
                    "is_new": params.get("is_new"),
                }
            ]
        )


def test_multi_level_publish_and_hierarchy(tmp_path):
    provider = Provider()
    history = download_industries(root=tmp_path / "industries", provider=provider, levels=("L1", "L2", "L3"))
    assert set(history.levels) == {"L1", "L2", "L3"}
    assert history.levels["L3"]["sectors"] == 2
    assert history.manifest["schema_version"] == "sw-history-v2"
    sectors = history.sectors
    assert set(sectors.level) == {"L1", "L2", "L3"}
    l3 = sectors.loc[sectors.level.eq("L3")].iloc[0]
    assert l3["parent_code"] == "210100" or l3["parent_code"] == "110100"
    loaded = load_industries(history.manifest["id"], root=tmp_path / "industries")
    assert loaded.manifest == history.manifest
    assert loaded.levels == history.levels


def test_level_filtering_and_missing_level_are_explicit(tmp_path):
    provider = Provider()
    history = download_industries(root=tmp_path / "industries", provider=provider, levels=("L1", "L2"))
    calendar = pd.bdate_range("2022-01-03", "2022-01-10")
    l1 = history.at("2022-01-04", ["SZ000001"], calendar, level="L1")
    l2 = history.at("2022-01-04", ["SZ000001"], calendar, level="L2")
    assert l1["SZ000001"]["industry_code"] == "110000"
    assert l2["SZ000001"]["industry_code"] == "110100"
    assert l1["SZ000001"]["industry_level"] == "L1"
    with pytest.raises(ValueError, match="三级"):
        history.at("2022-01-04", ["SZ000001"], calendar, level="L3")
    with pytest.raises(ValueError, match="级别"):
        history.at("2022-01-04", ["SZ000001"], calendar, level="L4")


def test_legacy_v1_version_still_loads_as_l1(tmp_path):
    frame = pd.DataFrame(
        [
            {
                "instrument": "SZ000001",
                "industry_code": "801780.SI",
                "industry_name": "银行",
                "classification": "SW2021",
                "in_date": pd.Timestamp("1990-01-01"),
                "out_date": pd.NaT,
            }
        ]
    )
    history = publish_industries(frame, root=tmp_path / "industries")
    assert history.manifest["schema_version"] == "sw-l1-history-v1"
    assert history.levels == {"L1": {"label": "一级", "rows": 1, "instruments": 1, "sectors": 1}}
    calendar = pd.bdate_range("2022-01-03", "2022-01-10")
    assert history.at("2022-01-04", ["SZ000001"], calendar)["SZ000001"]["industry_status"] == "known"
    with pytest.raises(ValueError, match="二级"):
        history.at("2022-01-04", ["SZ000001"], calendar, level="L2")


def test_download_pause_resumes_from_the_cache(tmp_path):
    provider = Provider()
    root = tmp_path / "industries"
    pause_file = root / "PAUSE"

    class Pausing(Provider):
        def query(self, api, **params):
            frame = super().query(api, **params)
            if api == "index_member_all" and params.get("is_new") == "Y":
                pause_file.write_text("pause", encoding="utf-8")
            return frame

    with pytest.raises(DownloadPaused):
        download_industries(root=root, provider=Pausing(), levels=("L1",), pause_file=pause_file)
    assert optional_industries(root=root) is None
    pause_file.unlink()
    history = download_industries(root=root, provider=provider, levels=("L1",), pause_file=pause_file)
    assert history.manifest["schema_version"] == "sw-history-v2"


def test_failed_sector_does_not_publish_and_reports_the_partition(tmp_path):
    root = tmp_path / "industries"
    provider = Provider(fail=lambda api, params: api == "index_member_all" and params.get("is_new") == "N")
    with pytest.raises(IndustryDownloadIncomplete) as caught:
        download_industries(root=root, provider=provider, levels=("L1",))
    assert caught.value.failures
    assert optional_industries(root=root) is None


def test_neutralization_level_roundtrip_and_context_validation(tmp_path):
    provider = Provider()
    history = download_industries(root=tmp_path / "industries", provider=provider, levels=("L1", "L2"))
    config = NeutralizationConfig("industry", history.manifest["id"], "L2")
    assert config.as_dict()["industry_level"] == "L2"
    assert parse_neutralization(config.as_dict()) == config
    assert "industry_level" not in NeutralizationConfig("industry", history.manifest["id"]).as_dict()
    assert industry_context(config, history=history) is history
    with pytest.raises(ValueError, match="三级"):
        industry_context(NeutralizationConfig("industry", history.manifest["id"], "L3"), history=history)
    with pytest.raises(ValueError):
        NeutralizationConfig("industry", history.manifest["id"], "L4")


def test_sectors_are_validated_not_inferred():
    with pytest.raises(ValueError, match="目录为空"):
        normalize_sectors([("SW2021", "L1", pd.DataFrame())])
    with pytest.raises(ValueError, match="级别"):
        normalize_sectors([("SW2021", "L9", pd.DataFrame([{"index_code": "x", "industry_name": "y"}]))])
    with pytest.raises(ValueError, match="代码或名称"):
        normalize_sectors([("SW2021", "L1", pd.DataFrame([{"industry_name": "y"}]))])


def test_market_factors_uses_the_selected_level():

    market = frame(4, 70)
    symbols = sorted(market.instrument.unique())
    l1 = ["A", "A", "B", "B"]
    l2 = ["X", "Y", "X", "Y"]
    rows = []
    for symbol, first, second in zip(symbols, l1, l2, strict=True):
        for level, code in (("L1", first), ("L2", second)):
            rows.append(
                {
                    "instrument": symbol,
                    "classification": "SW2021",
                    "level": level,
                    "industry_code": code,
                    "industry_name": code,
                    "parent_code": None,
                    "in_date": pd.Timestamp("2010-01-01"),
                    "out_date": pd.NaT,
                }
            )
    history = IndustryHistory(
        pd.DataFrame(rows), {"id": "a" * 64, "source": "fixture", "policy": "fixture", "point_in_time_certified": False}
    )
    config = FactorConfig((FactorSpec("book_to_price", 1.0),))
    first = market_factors(
        market, "fixture", config, industry_history=history, neutralization=NeutralizationConfig("industry", None, "L1")
    )
    second = market_factors(
        market, "fixture", config, industry_history=history, neutralization=NeutralizationConfig("industry", None, "L2")
    )
    day = first["metadata"]["as_of"]
    left = {row["instrument"]: row["scores"]["book_to_price"] for row in first["history"] if row["date"] == day}
    right = {row["instrument"]: row["scores"]["book_to_price"] for row in second["history"] if row["date"] == day}
    assert left != right
    assert first["metadata"]["industry_data"]["level"] == "L1"
    assert second["metadata"]["industry_data"]["level"] == "L2"
