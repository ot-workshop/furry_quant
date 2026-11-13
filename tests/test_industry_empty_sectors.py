"""Empty SW2021 classifications stay visible without blocking cached downloads."""

import json

import pandas as pd
import pytest
from test_industry_download import Provider

from finance_quant.application import industry_download
from finance_quant.application.task_worker import run_industry_download_task
from finance_quant.checksums import file_sha256
from finance_quant.data import industries
from finance_quant.data.managed import atomic_json
from finance_quant.data.providers.tushare import TushareProviderError
from support.fakes import make_service

EMPTY_SECTORS = [
    ("L2", "801217.SI", "本地生活服务Ⅱ"),
    ("L2", "801786.SI", "其他银行Ⅱ"),
    ("L2", "801768.SI", "社交Ⅱ"),
    ("L3", "851332.SI", "多品类奢侈品"),
    ("L3", "851544.SI", "互联网药店"),
    ("L3", "852171.SI", "本地生活服务Ⅲ"),
    ("L3", "859931.SI", "博彩"),
    ("L3", "857861.SI", "其他银行Ⅲ"),
    ("L3", "857672.SI", "音频媒体"),
    ("L3", "857681.SI", "社交Ⅲ"),
    ("L3", "857693.SI", "其他出版"),
]


class EmptySectorProvider(Provider):
    def __init__(self, *, history_code=None, denied_code=None):
        super().__init__()
        self.history_code = history_code
        self.denied_code = denied_code

    def query(self, api, **params):
        if api == "index_classify":
            normal = super().query(api, **params)
            if params.get("src") != "SW2021":
                return normal
            extra = [
                {
                    "index_code": code,
                    "industry_name": name,
                    "level": level,
                    "industry_code": code[:6],
                    "parent_code": "110000" if level == "L2" else "110100",
                }
                for level, code, name in EMPTY_SECTORS
                if level == params["level"]
            ]
            return pd.concat([normal, pd.DataFrame(extra)], ignore_index=True) if extra else normal
        code = params.get("l2_code") or params.get("l3_code")
        if api == "index_member_all" and code in {row[1] for row in EMPTY_SECTORS}:
            if code == self.history_code and params["is_new"] == "N":
                return super().query(api, **params)
            self.calls.append((api, params))
            if code == self.denied_code:
                raise TushareProviderError("Access denied", category="access_denied")
            return pd.DataFrame()
        return super().query(api, **params)


class OfflineProvider:
    def query(self, *args, **kwargs):
        pytest.fail("Resume must reuse all existing cache files without requesting the provider")


def cache_old_failed_download(root, provider, levels):
    """Create the raw file layout used before empty-sector handling existed."""
    raw = root / "raw"
    raw.mkdir(parents=True)
    for taxonomy in ("SW2014", "SW2021"):
        for level in levels:
            params = {"level": level, **({"src": taxonomy} if taxonomy == "SW2021" else {})}
            catalog = provider.query("index_classify", **params)
            catalog.to_parquet(raw / f"{taxonomy}-{level}-catalog.parquet", index=False)
            for code in catalog.index_code:
                for state in ("Y", "N"):
                    parameter = "index_code" if taxonomy == "SW2014" else level.lower() + "_code"
                    api = "index_member" if taxonomy == "SW2014" else "index_member_all"
                    frame = provider.query(api, **{parameter: code, "is_new": state})
                    frame.to_parquet(raw / f"{taxonomy}-{level}-{code}-{state}.parquet", index=False)
    return {path.name: file_sha256(path) for path in raw.glob("*.parquet")}


@pytest.mark.parametrize("levels", [("L1", "L2", "L3"), ("L2",), ("L3",)])
def test_all_verified_empty_sectors_publish_without_losing_levels(tmp_path, levels):
    root = tmp_path / "industries"
    plan = industry_download.preview(
        {"levels": list(levels), "refresh": False}, root=root, provider=EmptySectorProvider()
    )
    events = []
    result = industry_download.run(
        root, plan["id"], provider=EmptySectorProvider(), on_progress=lambda row: events.append(dict(row))
    )
    expected = {(level, code) for level, code, _ in EMPTY_SECTORS if level in levels}
    assert result["status"] == "complete"
    assert result["completed"] == result["total"]
    assert not result["failures"]
    assert {(sector["level"], sector["code"]) for sector in result["empty_sectors"]} == expected
    assert all(sector["reason"] == "暂无A股成员" for sector in result["empty_sectors"])
    history = industries.load_industries(result["version_id"], root=root)
    assert set(history.levels) == set(levels)
    assert history.manifest["empty_sectors"] == result["empty_sectors"]
    empty = history.sectors.loc[history.sectors.member_status.eq("no_a_share_members")]
    assert len(empty) == len(expected)
    assert not set(empty.industry_code).intersection(history.frame.industry_code)
    # The final member event reaches the exact total before publication as well.
    assert events[-2]["completed"] == result["total"]
    assert all(event["completed"] <= result["total"] for event in events)
    status = industry_download.status(root)
    assert status["version"]["empty_sectors"] == result["empty_sectors"]
    assert status["versions"][0]["empty_sectors"] == result["empty_sectors"]
    assert status["jobs"][0]["empty_sectors"] == result["empty_sectors"]


def test_old_partial_cache_resumes_offline_through_the_task_worker(tmp_path, monkeypatch):
    root = tmp_path / "industries"
    levels = ["L1", "L2", "L3"]
    plan = industry_download.preview({"levels": levels, "refresh": False}, root=root, provider=EmptySectorProvider())
    before = cache_old_failed_download(root, EmptySectorProvider(), levels)
    atomic_json(
        root / "jobs" / plan["id"] / "progress.json",
        {
            "id": plan["id"],
            "status": "partial",
            "total": plan["estimated_requests"],
            "completed": plan["estimated_requests"],
            "levels": levels,
            "failures": [{"reason": "No objects to concatenate"}],
        },
    )
    monkeypatch.setattr(industry_download, "default_provider", OfflineProvider)
    queue = make_service(tmp_path / "runs", tmp_path / "data").store
    submitted = industry_download.start_task(queue, root=root, plan_id=plan["id"])
    assert queue.claim() == submitted["id"]
    run_industry_download_task(queue, queue.get(submitted["id"], private=True))
    task = queue.get(submitted["id"])
    assert task["status"] == "succeeded", task["error"]
    assert len(task["result"]["empty_sectors"]) == 11
    assert {path.name: file_sha256(path) for path in (root / "raw").glob("*.parquet")} == before
    status = industry_download.status(root)
    assert status["jobs"][0]["status"] == "complete"
    assert not status["jobs"][0]["failures"]
    assert "暂无A股成员" in status["jobs"][0]["message"]
    version = status["version"]["id"]
    again = industry_download.run(root, plan["id"], provider=OfflineProvider())
    assert again["version_id"] == version
    assert industries.load_industries(version, root=root).manifest["empty_sectors"] == again["empty_sectors"]


def test_historical_members_are_preserved_even_for_a_known_empty_classification(tmp_path):
    code = "851544.SI"
    history = industries.download_industries(
        root=tmp_path / "industries", provider=EmptySectorProvider(history_code=code), levels=("L1", "L2", "L3")
    )
    assert code not in {row["code"] for row in history.manifest["empty_sectors"]}
    sector = history.sectors.loc[history.sectors.index_code.eq(code)].iloc[0]
    assert sector.member_status == "available"
    assert not history.frame.loc[history.frame.industry_code.eq(sector.industry_code)].empty


def test_access_denied_for_a_known_empty_sector_still_fails(tmp_path):
    root = tmp_path / "industries"
    plan = industry_download.preview({"levels": ["L2"], "refresh": False}, root=root, provider=EmptySectorProvider())
    result = industry_download.run(root, plan["id"], provider=EmptySectorProvider(denied_code="801217.SI"))
    assert result["status"] == "partial"
    assert {row["code"] for row in result["failures"]} == {"801217.SI"}
    assert "801217.SI" not in {row["code"] for row in result["empty_sectors"]}
    assert industries.optional_industries(root=root) is None


def test_unverified_empty_sector_keeps_the_previous_version(tmp_path):
    root = tmp_path / "industries"
    previous = industries.download_industries(root=root, provider=Provider(), levels=("L1", "L2", "L3"))

    class UnexpectedEmpty(Provider):
        def query(self, api, **params):
            if api == "index_member_all" and params.get("l3_code") == "850111.SI":
                return pd.DataFrame()
            return super().query(api, **params)

    plan = industry_download.preview(
        {"levels": ["L1", "L2", "L3"], "refresh": True}, root=root, provider=UnexpectedEmpty()
    )
    result = industry_download.run(root, plan["id"], provider=UnexpectedEmpty())
    assert result["status"] == "partial"
    assert len(result["failures"]) == 1
    assert "尚未确认" in result["failures"][0]["reason"]
    assert "No objects to concatenate" not in json.dumps(result)
    assert industries.load_industries(root=root).manifest["id"] == previous.manifest["id"]


def test_noncanonical_vendor_codes_are_reported_and_never_remapped(tmp_path):
    class LegacyCodeProvider(Provider):
        def query(self, api, **params):
            frame = super().query(api, **params)
            if api == "index_classify":
                return frame
            if params["is_new"] != ("N" if api == "index_member" else "Y"):
                return frame
            row = frame.iloc[0].to_dict()
            row["con_code" if api == "index_member" else "ts_code"] = "T00018.SH"
            row["in_date"] = "20000719"
            row["out_date"] = "20061020" if api == "index_member" else None
            return pd.concat([frame, pd.DataFrame([row])], ignore_index=True)

    root = tmp_path / "industries"
    plan = industry_download.preview(
        {"levels": ["L1", "L2", "L3"], "refresh": False}, root=root, provider=LegacyCodeProvider()
    )
    result = industry_download.run(root, plan["id"], provider=LegacyCodeProvider())
    assert result["status"] == "complete"
    assert len(result["excluded_members"]) == 6
    assert {row["code"] for row in result["excluded_members"]} == {"T00018.SH"}
    history = industries.load_industries(root=root)
    assert history.manifest["excluded_members"] == result["excluded_members"]
    assert set(history.frame.instrument) == {"SZ000001"}
    assert "SH600018" not in history.frame.instrument.values
    assert "未纳入研究" in result["message"]
    assert industry_download.status(root)["version"]["excluded_members"] == result["excluded_members"]
    raw = pd.read_parquet(root / "raw/SW2021-L3-850111.SI-Y.parquet")
    assert "T00018.SH" in raw.ts_code.values
