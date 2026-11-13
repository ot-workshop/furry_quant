"""Financial download planning, resumability and request pinning."""

import json
from pathlib import Path

import pandas as pd
import pytest

from finance_quant.application import financial_download
from finance_quant.application.financial_download import MAX_STATEMENT_ROWS
from finance_quant.application.research_requests import (
    pin_financials_for,
    request_config,
)
from finance_quant.data.financials import FinancialBinding, financial_root
from finance_quant.research.factor_library import FACTOR_LIBRARY
from finance_quant.research.registry import FactorConfig, FactorSpec
from support.financial import income, publish, raw_rows

FUNDAMENTAL = tuple(name for name, definition in FACTOR_LIBRARY.items() if definition.fundamental)


class Provider:
    """Returns canned statement responses and counts requests."""

    provider_id = "tushare_official"

    def __init__(self, rows_by_type=None, fail=None):
        self.calls = []
        self.rows_by_type = rows_by_type or {}
        self.fail = fail

    def query(self, api_name, *, fields="", **params):
        self.calls.append((api_name, params))
        if self.fail and self.fail(api_name, params):
            raise RuntimeError("供应商暂时不可用")
        if api_name == "trade_cal":
            days = pd.date_range(params["start_date"], params["end_date"])
            return pd.DataFrame({"cal_date": days.strftime("%Y%m%d"), "is_open": (days.dayofweek < 5).astype(int)})
        statement = {"income": "income", "balancesheet": "balance", "cashflow": "cashflow"}[api_name]
        report_type = params["report_type"]
        overrides = self.rows_by_type.get((statement, report_type), {})
        return raw_rows(
            ts_code=params["ts_code"],
            end_date="20241231",
            ann_date="20250320",
            f_ann_date="20250320",
            report_type=report_type,
            **overrides,
        )


def market_version(data_root: Path, codes=("600000.SH", "000001.SZ")) -> str:
    """A published shared-market version manifest with the given assets."""
    root = data_root / "shared-market" / "versions" / "shared-v1"
    (root / "reference").mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "shared-incremental-v1",
                "id": "shared-v1",
                "assets": {code: {"start": "2024-01-02", "end": "2025-06-30", "sources": []} for code in codes},
            }
        ),
        encoding="utf-8",
    )
    (root / "version.json").write_text(
        json.dumps({"id": "shared-v1", "scope": "custom", "codes_count": len(codes)}), encoding="utf-8"
    )
    return "shared-v1"


def test_preview_reports_pending_stocks_and_request_estimate(tmp_path):
    version = market_version(tmp_path)
    plan = financial_download.preview(tmp_path, {"market_version": version, "refresh_days": 7})
    assert plan["spec"]["market_version"] == version
    assert [row["code"] for row in plan["codes"]] == ["000001.SZ", "600000.SH"]
    assert plan["estimated_requests"] == 2 * 9
    assert plan["skipped"] == 0


def test_preview_skips_recently_fetched_stocks(tmp_path):
    version = market_version(tmp_path)
    publish(financial_root(tmp_path), [income(), income(ts_code="000001.SZ")])
    plan = financial_download.preview(tmp_path, {"market_version": version, "refresh_days": 3650})
    assert plan["skipped"] == 2
    assert plan["codes"] == []


def test_download_runs_nine_requests_per_stock_and_publishes_a_version(tmp_path):
    version = market_version(tmp_path, ("600000.SH",))
    plan = financial_download.preview(tmp_path, {"market_version": version, "refresh_days": 0})
    provider = Provider()
    result = financial_download.run(tmp_path, plan["id"], provider=provider)
    assert result["status"] == "complete", result
    assert result["completed"] == 1
    statements = {call[0] for call in provider.calls}
    assert statements == {"trade_cal", "income", "balancesheet", "cashflow"}
    assert len([c for c in provider.calls if c[0] != "trade_cal"]) == 9
    manifest = financial_download.FinancialStore(financial_root(tmp_path)).current()
    assert list(manifest["stocks"]) == ["600000.SH"]


def test_failed_stock_keeps_the_last_version_and_stays_resumable(tmp_path):
    version = market_version(tmp_path, ("600000.SH", "000001.SZ"))
    plan = financial_download.preview(tmp_path, {"market_version": version, "refresh_days": 0})
    provider = Provider(fail=lambda api, params: api == "cashflow" and params["ts_code"] == "000001.SZ")
    result = financial_download.run(tmp_path, plan["id"], provider=provider)
    assert result["status"] == "partial"
    assert [row["code"] for row in result["failures"]] == ["000001.SZ"]
    assert financial_download.FinancialStore(financial_root(tmp_path)).current() is None


def test_pause_keeps_completed_stocks_and_resumes(tmp_path):
    version = market_version(tmp_path, ("000001.SZ", "600000.SH", "600519.SH"))
    plan = financial_download.preview(tmp_path, {"market_version": version, "refresh_days": 0})
    root = financial_root(tmp_path)

    later = sorted({"000001.SZ", "600000.SH", "600519.SH"})[1]

    class Pausing(Provider):
        def query(self, api_name, *, fields="", **params):
            if api_name == "income" and params.get("ts_code") == later:
                (root / "PAUSE").write_text("pause", encoding="utf-8")
            return super().query(api_name, fields=fields, **params)

    result = financial_download.run(tmp_path, plan["id"], provider=Pausing())
    assert result["status"] == "paused"
    assert result["completed"] == 1
    assert (root / "stocks" / "000001.SZ.json").exists()
    assert not (root / "stocks" / "600519.SH.json").exists()
    resumed = financial_download.run(tmp_path, plan["id"], provider=Provider())
    assert resumed["status"] == "complete"


def test_statement_row_cap_forces_window_paging(tmp_path):
    """``balancesheet`` truncates at 100 rows; the downloader must page, never publish a cut history."""

    class Capped(Provider):
        def query(self, api_name, *, fields="", **params):
            frame = super().query(api_name, fields=fields, **params)
            if api_name != "balancesheet":
                return frame
            if int(params["end_date"][:4]) - int(params["start_date"][:4]) > 10:
                return pd.concat([frame] * MAX_STATEMENT_ROWS, ignore_index=True)
            # A real window holds far fewer rows than the cap.
            frame["end_date"] = params["start_date"][:4] + "1231"
            return frame

    version = market_version(tmp_path, ("600000.SH",))
    plan = financial_download.preview(tmp_path, {"market_version": version, "refresh_days": 0})
    provider = Capped()
    result = financial_download.run(tmp_path, plan["id"], provider=provider)
    assert result["status"] == "complete", result
    windows = [call for call in provider.calls if call[0] == "balancesheet" and "start_date" in call[1]]
    assert len(windows) > 2
    assert windows[0][1]["start_date"] == "19900101"


def test_impossible_window_is_reported_instead_of_truncated(tmp_path):
    class AlwaysFull(Provider):
        def query(self, api_name, *, fields="", **params):
            frame = super().query(api_name, fields=fields, **params)
            return frame if api_name != "balancesheet" else pd.concat([frame] * MAX_STATEMENT_ROWS, ignore_index=True)

    version = market_version(tmp_path, ("600000.SH",))
    plan = financial_download.preview(tmp_path, {"market_version": version, "refresh_days": 0})
    result = financial_download.run(tmp_path, plan["id"], provider=AlwaysFull())
    assert result["status"] == "partial"
    assert any("上限" in row["reason"] for row in result["failures"])


def test_plan_is_content_addressed_and_validated(tmp_path):
    version = market_version(tmp_path)
    spec = {"market_version": version, "refresh_days": 1}
    first = financial_download.preview(tmp_path, spec)
    assert financial_download.preview(tmp_path, spec)["id"] == first["id"]
    path = financial_root(tmp_path) / "jobs" / first["id"] / "plan.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["estimated_requests"] = 1
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="校验失败"):
        financial_download.load_plan(tmp_path, first["id"])


def test_invalid_specs_are_rejected(tmp_path):
    with pytest.raises(ValueError):
        financial_download.preview(tmp_path, {"market_version": "shared-v1"})
    with pytest.raises(ValueError):
        financial_download.preview(tmp_path, {"market_version": "shared-v1", "refresh_days": -1})
    with pytest.raises(ValueError):
        financial_download.preview(tmp_path, {"market_version": "../../etc", "refresh_days": 1})


def test_fundamental_factors_require_a_published_version(tmp_path):
    with pytest.raises(ValueError, match="财务数据"):
        pin_financials_for([FactorConfig((FactorSpec("roe_ttm", 1.0),))], tmp_path)


def test_request_pins_the_current_version_and_skips_it_for_price_factors(tmp_path, workspace):
    data_root, runs_root, request = workspace
    manifest = publish(
        financial_root(data_root),
        [income(ts_code=code) for code in ("600000.SH", "000001.SZ")],
    )
    price_only = FactorConfig((FactorSpec("momentum", 1.0, 20),))
    assert pin_financials_for([price_only], data_root) is None
    fundamental = FactorConfig((FactorSpec("roe_ttm", 1.0),))
    binding = pin_financials_for([price_only, fundamental], data_root)
    assert binding.version == manifest["id"]
    pinned = {
        "name": "财报研究",
        "data": {"mode": "snapshot", "snapshot_id": request["data"]["snapshot_id"], "codes": request["data"]["codes"]},
        "backtest": request["backtest"],
        "factors": fundamental.as_list(),
        "evaluation": request["evaluation"],
        "financials": {"version": binding.version},
    }
    config = request_config(pinned, _data(request, data_root), runs_root, data_root)
    assert isinstance(config.financials, FinancialBinding)
    assert config.financials.version == binding.version


def _data(request, data_root):
    from finance_quant.application.research_requests import snapshot_registry

    manifest = snapshot_registry(data_root)[request["data"]["snapshot_id"]][3]
    from finance_quant.application.experiment_tasks import _data_config

    return _data_config(manifest, request["data"]["codes"])
