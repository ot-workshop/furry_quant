"""Industry download planning, execution and status; API surface for the data page."""

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from finance_quant.application import industry_download
from finance_quant.data import industries
from finance_quant.data.industries import industry_root
from support.fakes import make_app, make_service


class Provider:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    def query(self, api, **params):
        self.calls.append((api, params))
        if api == "index_classify":
            level = params["level"]
            taxonomy = params.get("src", "SW2014")
            code = {"L1": "801010.SI", "L2": "801016.SI", "L3": "850111.SI"}[level]
            industry = {"L1": "110000", "L2": "110100", "L3": "110101"}[level]
            parent = {"L1": "0", "L2": "110000", "L3": "110100"}[level]
            return pd.DataFrame(
                [
                    {
                        "index_code": code + ("" if taxonomy == "SW2021" else ""),
                        "industry_name": f"{taxonomy}-{level}",
                        "level": level,
                        "industry_code": industry,
                        "parent_code": parent,
                    }
                ]
            )
        if self.fail and self.fail(api, params):
            raise RuntimeError("vendor failed")
        code = params.get("index_code") or params.get("l1_code") or params.get("l2_code") or params.get("l3_code")
        if api == "index_member":
            return pd.DataFrame(
                [{"index_code": code, "con_code": "000001.SZ", "in_date": "19900101", "out_date": None}]
            )
        return pd.DataFrame(
            [
                {
                    "l1_code": params.get("l1_code", "801010.SI"),
                    "l1_name": "一级",
                    "l2_code": params.get("l2_code", "801016.SI"),
                    "l2_name": "二级",
                    "l3_code": params.get("l3_code", "850111.SI"),
                    "l3_name": "三级",
                    "ts_code": "000001.SZ",
                    "name": "平安银行",
                    "in_date": "19900101",
                    "out_date": None,
                }
            ]
        )


def test_preview_estimates_requests_from_catalogs(tmp_path):
    plan = industry_download.preview(
        {"levels": ["L1", "L2"], "refresh": False},
        root=tmp_path / "industries",
        provider=Provider(),
    )
    assert plan["spec"]["levels"] == ["L1", "L2"]
    assert plan["sectors"]["SW2014-L1"] == 1
    assert plan["estimated_requests"] > 0
    assert plan["estimated_minutes"] >= 1
    assert industry_download.load_plan(tmp_path / "industries", plan["id"]) == plan


def test_preview_rejects_invalid_levels(tmp_path):
    with pytest.raises(ValueError):
        industry_download.preview({"levels": [], "refresh": False}, root=tmp_path, provider=Provider())
    with pytest.raises(ValueError):
        industry_download.preview({"levels": ["L9"], "refresh": False}, root=tmp_path, provider=Provider())
    with pytest.raises(ValueError):
        industry_download.preview({"levels": ["L1"], "refresh": "yes"}, root=tmp_path, provider=Provider())


def test_run_publishes_a_complete_version(tmp_path):
    root = tmp_path / "industries"
    plan = industry_download.preview({"levels": ["L1", "L2"], "refresh": False}, root=root, provider=Provider())
    progress = industry_download.run(root, plan["id"], provider=Provider())
    assert progress["status"] == "complete"
    assert progress["completed"] == progress["total"]
    assert progress["version_id"]
    assert industries.load_industries(progress["version_id"], root=root).levels.keys() == {"L1", "L2"}


def test_run_reports_a_partial_without_publishing(tmp_path):
    root = tmp_path / "industries"
    plan = industry_download.preview({"levels": ["L1"], "refresh": False}, root=root, provider=Provider())
    provider = Provider(fail=lambda api, params: api == "index_member_all" and params.get("is_new") == "N")
    progress = industry_download.run(root, plan["id"], provider=provider)
    assert progress["status"] == "partial"
    assert progress["failures"]
    assert industries.optional_industries(root=root) is None


def test_run_can_be_paused_and_resumed(tmp_path):
    root = tmp_path / "industries"
    plan = industry_download.preview({"levels": ["L1"], "refresh": False}, root=root, provider=Provider())

    class Pausing(Provider):
        def query(self, api, **params):
            frame = super().query(api, **params)
            if api == "index_member_all" and params.get("is_new") == "Y":
                (root / "PAUSE").write_text("pause", encoding="utf-8")
            return frame

    paused = industry_download.run(root, plan["id"], provider=Pausing())
    assert paused["status"] == "paused"
    assert industries.optional_industries(root=root) is None
    resumed = industry_download.run(root, plan["id"], provider=Provider())
    assert resumed["status"] == "complete"


def test_status_and_start_task(tmp_path, monkeypatch):
    store = make_service(tmp_path / "runs", tmp_path / "data").store
    root = tmp_path / "industries"
    plan = industry_download.preview({"levels": ["L1"], "refresh": False}, root=root, provider=Provider())
    status = industry_download.status(root)
    assert status["version"] is None and status["jobs"] == []
    task = industry_download.start_task(store, root=root, plan_id=plan["id"])
    assert task["kind"] == "industry_download"
    assert task["status"] == "queued"


def test_api_status_and_pause(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_QUANT_INDUSTRY_ROOT", str(tmp_path / "industries"))
    service = make_service(tmp_path / "runs", tmp_path / "data")
    client = TestClient(
        make_app(tmp_path / "runs", data_root=tmp_path / "data", service=service), base_url="http://127.0.0.1"
    )
    status = client.get("/api/v1/research/industry-download")
    assert status.status_code == 200
    assert status.json()["version"] is None
    response = client.post("/api/v1/research/industry-download/pause", json={})
    assert response.status_code == 200
    assert (industry_root() / "PAUSE").exists()


def test_api_preview_and_start_with_a_fake_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_QUANT_INDUSTRY_ROOT", str(tmp_path / "industries"))
    monkeypatch.setattr(industry_download, "default_provider", Provider)
    service = make_service(tmp_path / "runs", tmp_path / "data")
    client = TestClient(
        make_app(tmp_path / "runs", data_root=tmp_path / "data", service=service), base_url="http://127.0.0.1"
    )
    preview = client.post(
        "/api/v1/research/industry-download/preview",
        json={"levels": ["L1"], "refresh": False},
    )
    assert preview.status_code == 200, preview.text
    plan_id = preview.json()["id"]
    started = client.post("/api/v1/research/industry-download/start", json={"plan_id": plan_id})
    assert started.status_code == 200, started.text
    assert started.json()["task_id"]
    task = service.store.get(started.json()["task_id"])
    assert task["kind"] == "industry_download"


def test_research_options_expose_levels_and_pin_the_current_version(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_QUANT_INDUSTRY_ROOT", str(tmp_path / "industries"))
    history = industries.download_industries(root=industry_root(), provider=Provider(), levels=("L1", "L2"))
    service = make_service(tmp_path / "runs", tmp_path / "data")
    client = TestClient(
        make_app(tmp_path / "runs", data_root=tmp_path / "data", service=service), base_url="http://127.0.0.1"
    )
    options = client.get("/api/v1/research/options").json()
    industry = options["industry_data"]
    assert industry["id"] == history.manifest["id"]
    assert set(industry["levels"]) == {"L1", "L2"}
    assert industry["levels"]["L2"]["sectors"] >= 1
