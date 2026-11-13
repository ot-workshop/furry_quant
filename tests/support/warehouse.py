"""Test harness: a single-directory CSI300 warehouse download around ``download_asset``.

Production downloads CSI300 history through the shared market download (scope ``csi300``),
which publishes ``versions/*/index-view``. This harness keeps the per-asset validation
policies (primary/secondary sampling, official suspensions, repairs) testable in isolation.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd
from filelock import FileLock

from finance_quant.application import quote_capture
from finance_quant.application.quote_capture import WAREHOUSE_POLICY, download_asset, validation_signature
from finance_quant.application.update_service import publish_references
from finance_quant.checksums import file_sha256, sha256_text
from finance_quant.data import shared_market
from finance_quant.data.catalog import Catalog, utc_now
from finance_quant.data.index_universe import members_on
from finance_quant.data.managed import ManagedFetcher, atomic_json
from finance_quant.data.network_budget import DownloadPaused
from finance_quant.data.providers.baostock import BaoStockProvider, BaoStockProviderError

__all__ = ["index_observations_on", "quote_capture", "run_index_download"]


def run_index_download(
    root, start="2020-01-01", end=None, warmup=127, *, prepare_only=False, limit=None, provider=None, secondary=None
):
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        raise ValueError("本次股票数量须为正整数")
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with FileLock(root / "warehouse.lock", timeout=0):
        plan_path = root / "plan.json"
        if plan_path.exists():
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            if (
                pd.Timestamp(plan["requested_start"]) != pd.Timestamp(start)
                or plan["warmup_sessions"] != warmup
                or (end is not None and plan["requested_end"] != end)
            ):
                raise ValueError("现有冻结下载计划与参数不同，请使用新数据目录")
            for name, digest in plan["reference_files"].items():
                if file_sha256(root / "reference" / name) != digest:
                    raise ValueError("参考数据校验失败")
        else:
            raise FileNotFoundError("Test warehouse needs a prepared plan.json")
        if prepare_only:
            return plan

        plan_id = sha256_text(json.dumps(plan, sort_keys=True))[:24]
        publish_references(root, {**plan, "id": plan_id})
        catalog = Catalog(root)
        job = "index-" + str(time.time_ns())
        catalog.begin_job(job, {"index": plan["index"], "codes": plan["codes"]}, len(plan["tasks"]) * 4)
        fetch = ManagedFetcher(provider, root, catalog, job, False, {"validation": {"mode": "fallback"}})
        fetch.validation_sha256 = validation_signature()
        if fetch.provider_id != plan["provider_id"]:
            raise ValueError("续传数据源发生变化")
        secondary = secondary or BaoStockProvider(root / "secondary", timeout=90)
        calendar = pd.read_parquet(root / "reference/calendar.parquet")
        intervals = pd.read_parquet(root / "reference/memberships.parquet")
        progress_path = root / "progress.json"
        old = json.loads(progress_path.read_text(encoding="utf-8")) if progress_path.exists() else {}
        done = {
            code: record
            for code, record in old.get("assets", {}).items()
            if record.get("validation_sha256") == fetch.validation_sha256
        }
        failures = dict(old.get("failures", {}))
        progress = {
            "status": "running",
            "job_id": job,
            "total": len(plan["tasks"]),
            "assets": done,
            "failures": failures,
            "updated_at": utc_now(),
        }

        def save():
            progress["completed"] = len(done)
            progress["updated_at"] = utc_now()
            progress["validation_counts"] = {
                level: sum(r.get("validation_level") == level for r in done.values())
                for level in ["single_source_passed", "dual_source_verified", "secondary_fallback_passed"]
            }
            progress["validation_policy"] = WAREHOUSE_POLICY
            progress["raw_captures"] = len(
                {
                    r["code"]
                    for r in (json.loads(p.read_text(encoding="utf-8")) for p in (root / "captures").glob("*.json"))
                    if r["responses_saved"] == 4
                }
            )
            atomic_json(progress_path, progress)

        try:
            save()
            consecutive_connections = 0
            for task in plan["tasks"][:limit] if limit else plan["tasks"]:
                if validation_signature() != fetch.validation_sha256:
                    raise DownloadPaused("校验代码已更新，已保留断点；请续传以使用新版本")
                if (root / "PAUSE").exists():
                    raise DownloadPaused("用户暂停了指数下载；移除PAUSE文件后可续传")
                progress["current_code"] = task["code"]
                save()
                try:
                    record = download_asset(root, task, calendar, intervals, fetch, secondary)
                    shared_market.register_segment(
                        root, record, root / record["file"], batch_id=f"index-{plan_id}-{record['key']}"
                    )
                    done[task["code"]] = {
                        k: record[k]
                        for k in [
                            "key",
                            "code",
                            "start",
                            "end",
                            "rows",
                            "file",
                            "sha256",
                            "provider_id",
                            "validation_level",
                            "validation_sha256",
                        ]
                    }
                    failures.pop(task["code"], None)
                    consecutive_connections = 0
                    print(f"INDEX {len(done)}/{len(plan['tasks'])} {task['code']} verified", flush=True)
                except DownloadPaused:
                    raise
                except Exception as error:
                    done.pop(task["code"], None)
                    failures[task["code"]] = {
                        "type": type(error).__name__,
                        "reason": str(error)[:300]
                        if isinstance(error, ValueError)
                        else "source_or_coverage_validation_failed",
                        "dates": getattr(error, "validation_dates", [])[:31],
                    }
                    print(f"INDEX {task['code']} failed: {type(error).__name__}", flush=True)

                    consecutive_connections = (
                        consecutive_connections + 1
                        if isinstance(error, BaoStockProviderError) and error.retryable
                        else 0
                    )
                    if consecutive_connections >= 3:
                        raise DownloadPaused("BaoStock连续三只股票连接失败，暂停避免持续重连；稍后可续传")
                save()
            progress["status"] = "complete" if len(done) == len(plan["tasks"]) and not failures else "partial"
            if progress["status"] == "complete":
                manifest = {
                    "schema_version": "historical-index-warehouse-v1",
                    "index": plan["index"],
                    "start": plan["start"],
                    "end": plan["end"],
                    "data_start": plan["data_start"],
                    "warmup_sessions": warmup,
                    "provider_id": fetch.provider_id,
                    "validation_sha256": fetch.validation_sha256,
                    "membership_policy": plan["membership_policy"],
                    "point_in_time_certified": False,
                    "reference_files": plan["reference_files"],
                    "assets": done,
                    "rows": sum(r["rows"] for r in done.values()),
                    "validation_policy": WAREHOUSE_POLICY,
                    "ready_for_legacy_fixed_pool_research": False,
                    "created_at": utc_now(),
                }
                atomic_json(root / "manifest.json", manifest)
            save()
            catalog.finish(
                job, progress["status"], {"completed": len(done), "total": len(plan["tasks"]), "failures": failures}
            )
            return progress
        except DownloadPaused as error:
            progress.update(status="paused", message=str(error))
            save()
            catalog.finish(job, "paused", {"completed": len(done)})
            return progress
        except BaseException:
            progress["status"] = "interrupted"
            save()
            catalog.finish(job, "interrupted", {"completed": len(done)})
            raise
        finally:
            catalog.close()


def index_observations_on(root, day, *, require_history=True):
    """A safe daily universe reader; never exposes a future constituent's row."""
    root = Path(root)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if not pd.Timestamp(manifest["start"]) <= pd.Timestamp(day) <= pd.Timestamp(manifest["end"]):
        raise ValueError("日期不在冻结数据区间")
    for name, digest in manifest["reference_files"].items():
        if file_sha256(root / "reference" / name) != digest:
            raise ValueError("参考数据校验失败")
    intervals = pd.read_parquet(root / "reference/memberships.parquet")
    selected = members_on(intervals, day)
    rows = []
    excluded = {}
    for code in selected:
        asset = manifest["assets"][code]
        path = (root / asset["file"]).resolve()
        if not pd.Timestamp(asset["start"]) <= pd.Timestamp(day) <= pd.Timestamp(asset["end"]):
            excluded[code] = "outside_stored_listing_lifecycle"
            continue
        if not path.is_relative_to(root.resolve()) or file_sha256(path) != asset["sha256"]:
            raise ValueError("股票数据校验失败")
        frame = pd.read_parquet(path, filters=[("date", "==", pd.Timestamp(day))])
        if len(frame) != 1:
            raise ValueError(f"{code}成分期报价缺失")
        row = frame.iloc[0]
        if require_history and not bool(row.has_required_pre_history):
            excluded[code] = "insufficient_pre_history"
            continue
        rows.append(row)
    return {
        "date": str(pd.Timestamp(day).date()),
        "pool_size": len(selected),
        "rows": pd.DataFrame(rows),
        "excluded": excluded,
        "membership_policy": manifest["membership_policy"],
    }
