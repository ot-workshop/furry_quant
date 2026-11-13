"""Versioned official SW industry intervals at L1/L2/L3; never backfill today's labels."""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from filelock import FileLock

from finance_quant.checksums import file_sha256, sha256_bytes
from finance_quant.data.managed import atomic_json
from finance_quant.data.network_budget import DownloadPaused
from finance_quant.data.providers.tushare import TushareProvider, TushareSettings
from finance_quant.data.symbols import TS_CODE, to_qlib
from finance_quant.paths import data_root

VERSION = "sw-l1-history-v1"
VERSION_V2 = "sw-history-v2"
LEVELS = ("L1", "L2", "L3")
LEVEL_LABELS = {"L1": "一级", "L2": "二级", "L3": "三级"}
TRANSITION = pd.Timestamp("2021-12-13")
LEGACY_START = pd.Timestamp("2015-01-01")
POLICY = (
    "申万一级；SW2014研究下界2015-01-01、截至2021-12-10，SW2021最早2021-12-13（保守版本切换约定，非公告认证）；"
    "记录纳入、剔除日期后的下一交易日起更新；不回填当前行业，冲突及缺失单列；"
    "日期为供应商事后历史记录，实际公告发布时间未认证"
)
POLICY_V2 = (
    "申万一级/二级/三级；SW2014研究下界2015-01-01、截至2021-12-10，SW2021最早2021-12-13（保守版本切换约定，非公告认证）；"
    "按行业分类目录保存层级关系；记录纳入、剔除日期后的下一交易日起更新；不回填当前行业，冲突及缺失单列；"
    "日期为供应商事后历史记录，实际公告发布时间未认证"
)
# A single sector response reaching this many rows may have been truncated by the vendor.
MAX_MEMBER_ROWS = 2000
# SW2021 classifications with no A-share constituents: official classification
# table https://tushare.pro/document/2?doc_id=181, rechecked on 2026-11-13.
# Only applies when BOTH current and historical member responses are empty.
EMPTY_A_SHARE_SECTORS = {
    ("L2", "801217.SI"),  # 本地生活服务Ⅱ
    ("L2", "801786.SI"),  # 其他银行Ⅱ
    ("L2", "801768.SI"),  # 社交Ⅱ
    ("L3", "851332.SI"),  # 多品类奢侈品
    ("L3", "851544.SI"),  # 互联网药店
    ("L3", "852171.SI"),  # 本地生活服务Ⅲ
    ("L3", "859931.SI"),  # 博彩
    ("L3", "857861.SI"),  # 其他银行Ⅲ
    ("L3", "857672.SI"),  # 音频媒体
    ("L3", "857681.SI"),  # 社交Ⅲ
    ("L3", "857693.SI"),  # 其他出版
}


class IndustryDownloadIncomplete(ValueError):
    """At least one sector failed; nothing was published and the cache is resumable."""

    def __init__(self, message, failures):
        super().__init__(message)
        self.failures = failures


def industry_root():
    configured = os.environ.get("FINANCE_QUANT_INDUSTRY_ROOT")
    if configured:
        return Path(configured)

    return data_root() / "industries/official-tushare"


def _today() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def normalize_intervals(parts):
    """Legacy L1 intervals; preserved for existing callers and immutable v1 archives."""
    rows = []
    for taxonomy, code, name, raw in parts:
        stock = "con_code" if taxonomy == "SW2014" else "ts_code"
        if raw.empty:
            continue
        if not {stock, "in_date", "out_date"} <= set(raw):
            raise ValueError("历史行业缺少日期或代码字段")
        for r in raw.to_dict("records"):
            symbol = str(r[stock])
            start = pd.to_datetime(r["in_date"], format="%Y%m%d", errors="coerce")
            end = pd.to_datetime(r["out_date"], format="%Y%m%d", errors="coerce")
            if pd.notna(r["out_date"]) and str(r["out_date"]).strip() and pd.isna(end):
                raise ValueError("历史行业剔除日期无效")
            if len(symbol) != 9 or symbol[-2:] not in {"SH", "SZ", "BJ"} or pd.isna(start):
                raise ValueError("历史行业代码或纳入日期无效")
            if pd.notna(end) and end < start:
                raise ValueError("历史行业剔除早于纳入")
            rows.append(
                dict(
                    instrument=to_qlib(symbol),
                    industry_code=code,
                    industry_name=name,
                    classification=taxonomy,
                    in_date=start,
                    out_date=end,
                )
            )
    if not rows:
        raise ValueError("历史行业数据为空")
    return (
        pd.DataFrame(rows)
        .drop_duplicates()
        .sort_values(["instrument", "classification", "in_date", "industry_code"])
        .reset_index(drop=True)
    )


def normalize_sectors(parts) -> pd.DataFrame:
    """One row per SW sector: taxonomy, level, index code, industry code, name and parent."""
    rows = []
    for taxonomy, level, catalog in parts:
        if level not in LEVELS:
            raise ValueError("历史行业级别无效")
        if not isinstance(catalog, pd.DataFrame) or catalog.empty:
            raise ValueError(f"{taxonomy} {level}行业分类目录为空")
        if not {"index_code", "industry_name"} <= set(catalog):
            raise ValueError("历史行业分类目录缺少代码或名称")
        for sector in catalog.to_dict("records"):
            if "level" in sector and pd.notna(sector["level"]) and str(sector["level"]) != level:
                raise ValueError("历史行业分类级别与请求不一致")
            code = str(sector["index_code"]).strip()
            if not code:
                raise ValueError("历史行业分类代码无效")
            industry_code = str(sector.get("industry_code") or code).strip()
            parent = str(sector.get("parent_code") or "").strip()
            rows.append(
                {
                    "classification": taxonomy,
                    "level": level,
                    "index_code": code,
                    "industry_code": industry_code,
                    "industry_name": str(sector["industry_name"]),
                    "parent_code": None if parent in {"", "0", "None", "nan"} else parent,
                }
            )
    if not rows:
        raise ValueError("历史行业分类目录为空")
    sectors = pd.DataFrame(rows).drop_duplicates(["classification", "level", "index_code"])
    if sectors.duplicated(["classification", "level", "industry_code"]).any():
        raise ValueError("历史行业分类代码重复")
    return sectors.sort_values(["classification", "level", "industry_code"]).reset_index(drop=True)


def normalize_level_intervals(parts, *, excluded_members=None) -> pd.DataFrame:
    """Intervals for every level. ``parts`` is ``(taxonomy, sector, raw_member_frame)``."""
    rows = []
    for taxonomy, sector, raw in parts:
        if raw is None or raw.empty:
            continue
        stock = "con_code" if taxonomy == "SW2014" else "ts_code"
        if not {stock, "in_date", "out_date"} <= set(raw):
            raise ValueError("历史行业缺少日期或代码字段")
        for record in raw.to_dict("records"):
            symbol = str(record[stock])
            if excluded_members is not None and not TS_CODE.fullmatch(symbol):
                excluded_members.append(
                    {
                        "taxonomy": taxonomy,
                        "level": sector["level"],
                        "sector_code": sector["index_code"],
                        "code": symbol,
                        "in_date": str(record["in_date"]) if pd.notna(record["in_date"]) else None,
                        "out_date": str(record["out_date"]) if pd.notna(record["out_date"]) else None,
                        "reason": "不支持的供应商股票代码；原始记录已保留，未纳入研究",
                    }
                )
                continue
            start = pd.to_datetime(record["in_date"], format="%Y%m%d", errors="coerce")
            end = pd.to_datetime(record["out_date"], format="%Y%m%d", errors="coerce")
            if pd.notna(record["out_date"]) and str(record["out_date"]).strip() and pd.isna(end):
                raise ValueError("历史行业剔除日期无效")
            if len(symbol) != 9 or symbol[-2:] not in {"SH", "SZ", "BJ"} or pd.isna(start):
                raise ValueError("历史行业代码或纳入日期无效")
            if pd.notna(end) and end < start:
                raise ValueError("历史行业剔除早于纳入")
            rows.append(
                {
                    "instrument": to_qlib(symbol),
                    "classification": taxonomy,
                    "level": sector["level"],
                    "industry_code": sector["industry_code"],
                    "industry_name": sector["industry_name"],
                    "parent_code": sector["parent_code"],
                    "in_date": start,
                    "out_date": end,
                }
            )
    if not rows:
        raise ValueError("历史行业数据为空")
    return (
        pd.DataFrame(rows)
        .drop_duplicates()
        .sort_values(["instrument", "classification", "level", "in_date", "industry_code"])
        .reset_index(drop=True)
    )


def _atomic_parquet(frame, path):
    descriptor, name = tempfile.mkstemp(dir=path.parent, suffix=".parquet")
    os.close(descriptor)
    temporary = Path(name)
    try:
        frame.to_parquet(temporary, index=False)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _levels_summary(frame, sectors) -> dict:
    summary = {}
    for level in LEVELS:
        part = frame.loc[frame.level.eq(level)]
        sector_part = sectors.loc[sectors.level.eq(level)]
        if part.empty:
            continue
        summary[level] = {
            "label": LEVEL_LABELS[level],
            "rows": len(part),
            "instruments": int(part.instrument.nunique()),
            "sectors": int(sector_part.index_code.nunique()),
        }
    return summary


def publish_industries(frame, *, root=None, evidence=None, data_asof=None, sectors=None, excluded_members=None):
    """Publish a v2 all-level version, or a legacy L1 version when no level information exists."""
    root = Path(root or industry_root())
    root.mkdir(parents=True, exist_ok=True)
    if sectors is None:
        frame = frame.drop(columns=["level", "parent_code"], errors="ignore")
        return _publish_legacy(frame, root=root, evidence=evidence, data_asof=data_asof)
    if "level" not in frame.columns:
        raise ValueError("多级行业数据必须同时提供级别和分类目录")
    frame = frame.copy()
    if not set(frame.level) <= set(LEVELS):
        raise ValueError("历史行业级别无效")
    if not isinstance(sectors, pd.DataFrame) or sectors.empty:
        raise ValueError("历史行业分类目录为空")
    if not set(sectors.classification) <= set(frame.classification) or not set(frame.level) <= set(sectors.level):
        raise ValueError("历史行业分类目录与成分级别不一致")
    data_asof = data_asof or _today()
    intervals_payload = frame.to_csv(index=False, date_format="%Y-%m-%d").encode()
    sectors_payload = sectors.to_csv(index=False, date_format="%Y-%m-%d").encode()
    digest = sha256_bytes(intervals_payload + sectors_payload + VERSION_V2.encode() + POLICY_V2.encode())
    directory = root / digest
    directory.mkdir(exist_ok=True)
    intervals_path = directory / "intervals.parquet"
    sectors_path = directory / "sectors.parquet"
    if not intervals_path.exists():
        _atomic_parquet(frame, intervals_path)
    if not sectors_path.exists():
        _atomic_parquet(sectors, sectors_path)
    manifest = {
        "id": digest,
        "schema_version": VERSION_V2,
        "file": "intervals.parquet",
        "sha256": file_sha256(intervals_path),
        "sectors_file": "sectors.parquet",
        "sectors_sha256": file_sha256(sectors_path),
        "rows": len(frame),
        "instruments": int(frame.instrument.nunique()),
        "levels": _levels_summary(frame, sectors),
        "source": "official_tushare",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "policy": POLICY_V2,
        "data_asof": data_asof,
        "point_in_time_certified": False,
        "evidence": evidence or {},
        "empty_sectors": [
            {
                "taxonomy": row.classification,
                "level": row.level,
                "code": row.index_code,
                "name": row.industry_name,
                "reason": "暂无A股成员",
            }
            for row in sectors.itertuples()
            if getattr(row, "member_status", None) == "no_a_share_members"
        ],
        "excluded_members": excluded_members or [],
    }
    if not (directory / "manifest.json").exists():
        atomic_json(directory / "manifest.json", manifest)
    atomic_json(root / "current.json", {"id": digest})
    return load_industries(root=root)


def _publish_legacy(frame, *, root, evidence=None, data_asof=None):
    payload = frame.to_csv(index=False, date_format="%Y-%m-%d").encode()
    data_asof = data_asof or _today()
    digest = sha256_bytes(payload + VERSION.encode() + POLICY.encode() + data_asof.encode())
    directory = root / digest
    directory.mkdir(exist_ok=True)
    path = directory / "intervals.parquet"
    if not path.exists():
        _atomic_parquet(frame, path)
    manifest = {
        "id": digest,
        "schema_version": VERSION,
        "file": "intervals.parquet",
        "sha256": file_sha256(path),
        "rows": len(frame),
        "instruments": int(frame.instrument.nunique()),
        "source": "official_tushare",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "policy": POLICY,
        "data_asof": data_asof,
        "point_in_time_certified": False,
        "evidence": evidence or {},
    }
    if not (directory / "manifest.json").exists():
        atomic_json(directory / "manifest.json", manifest)
    atomic_json(root / "current.json", {"id": digest})
    return load_industries(root=root)


def load_industries(version=None, *, root=None):
    root = Path(root or industry_root())
    if version is None:
        version = json.loads((root / "current.json").read_text(encoding="utf-8"))["id"]
    if not isinstance(version, str) or len(version) != 64 or any(c not in "0123456789abcdef" for c in version):
        raise ValueError("行业版本无效")
    directory = root / version
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("id") != version or manifest.get("point_in_time_certified") is not False:
        raise ValueError("历史行业版本或时点声明校验失败")
    schema = manifest.get("schema_version")
    if schema == VERSION:
        path = directory / "intervals.parquet"
        if file_sha256(path) != manifest["sha256"]:
            raise ValueError("历史行业缓存校验失败")
        frame = pd.read_parquet(path)
        payload = frame.to_csv(index=False, date_format="%Y-%m-%d").encode() + VERSION.encode()
        if "data_asof" in manifest:
            payload += manifest["policy"].encode() + manifest["data_asof"].encode()
        if sha256_bytes(payload) != version:
            raise ValueError("历史行业版本或时点声明校验失败")
        return IndustryHistory(frame, manifest)
    if schema != VERSION_V2:
        raise ValueError("不支持的历史行业版本")
    intervals_path = directory / "intervals.parquet"
    sectors_path = directory / "sectors.parquet"
    if file_sha256(intervals_path) != manifest["sha256"] or file_sha256(sectors_path) != manifest["sectors_sha256"]:
        raise ValueError("历史行业缓存校验失败")
    frame = pd.read_parquet(intervals_path)
    sectors = pd.read_parquet(sectors_path)
    payload = (
        frame.to_csv(index=False, date_format="%Y-%m-%d").encode()
        + sectors.to_csv(index=False, date_format="%Y-%m-%d").encode()
        + VERSION_V2.encode()
        + POLICY_V2.encode()
    )
    if sha256_bytes(payload) != version:
        raise ValueError("历史行业版本或时点声明校验失败")
    return IndustryHistory(frame, manifest, sectors)


def optional_industries(version=None, *, root=None):
    try:
        return load_industries(version, root=root)
    except FileNotFoundError:
        if version:
            raise ValueError("所选历史行业版本不存在，请重新下载历史行业数据") from None
        return None


class IndustryHistory:
    def __init__(self, frame, manifest, sectors=None):
        self.frame = frame.copy()
        if "level" not in self.frame:
            self.frame["level"] = "L1"
        if "parent_code" not in self.frame:
            self.frame["parent_code"] = None
        self.manifest = manifest
        self.sectors = sectors.copy() if isinstance(sectors, pd.DataFrame) else None
        self._cache = {}

    @property
    def data_asof(self):
        return self.manifest.get("data_asof") or (self.manifest.get("created_at") or "")[:10] or None

    @property
    def levels(self) -> dict:
        if self.manifest.get("levels"):
            return self.manifest["levels"]
        summary = {}
        for level in LEVELS:
            part = self.frame.loc[self.frame.level.eq(level)]
            if part.empty:
                continue
            summary[level] = {
                "label": LEVEL_LABELS[level],
                "rows": len(part),
                "instruments": int(part.instrument.nunique()),
                "sectors": int(part.industry_code.nunique()),
            }
        return summary

    def validate_range(self, end):
        if self.data_asof and pd.Timestamp(end) > pd.Timestamp(self.data_asof):
            raise ValueError(f"历史行业数据仅核验至{self.data_asof}，请刷新历史行业缓存后重试；不会继续套用旧标签")

    def validate_level(self, level):
        if level not in LEVELS:
            raise ValueError("历史行业级别须为 L1、L2 或 L3")
        if level not in self.levels:
            raise ValueError(f"历史行业版本不包含{LEVEL_LABELS[level]}行业，请重新下载或切换级别")

    def at(self, date, instruments, calendar, level="L1"):
        self.validate_level(level)
        date = pd.Timestamp(date)
        if date not in pd.DatetimeIndex(calendar):
            raise ValueError("行业匹配日期不在交易日历中")
        if date < LEGACY_START or (self.data_asof and date > pd.Timestamp(self.data_asof)):
            return {
                symbol: {
                    "industry_code": None,
                    "industry_name": None,
                    "classification": None,
                    "industry_level": level,
                    "industry_status": "missing",
                }
                for symbol in instruments
            }
        # Signals use only records dated strictly before this session. Date-only
        # changes are conservatively applied on the following exchange session.
        taxonomy = "SW2014" if date < TRANSITION else "SW2021"
        key = (date, taxonomy, level)
        if key not in self._cache:
            f = self.frame
            active = f.loc[
                f.classification.eq(taxonomy)
                & f.level.eq(level)
                & f.in_date.lt(date)
                & (f.out_date.isna() | f.out_date.ge(date))
            ]
            unique = active[["instrument", "industry_code", "industry_name"]].drop_duplicates()
            conflict = unique.instrument.duplicated(keep=False)
            mapped = {
                row.instrument: {
                    "industry_code": row.industry_code,
                    "industry_name": row.industry_name,
                    "classification": taxonomy,
                    "industry_level": level,
                    "industry_status": "known",
                }
                for row in unique.loc[~conflict].itertuples()
            }
            mapped.update(
                {
                    instrument: {
                        "industry_code": None,
                        "industry_name": None,
                        "classification": taxonomy,
                        "industry_level": level,
                        "industry_status": "conflict",
                    }
                    for instrument in unique.loc[conflict, "instrument"].unique()
                }
            )
            self._cache[key] = mapped
            if len(self._cache) > 8:
                self._cache.pop(next(iter(self._cache)))
        return {
            symbol: self._cache[key].get(
                symbol,
                {
                    "industry_code": None,
                    "industry_name": None,
                    "classification": taxonomy,
                    "industry_level": level,
                    "industry_status": "missing",
                },
            )
            for symbol in instruments
        }


def _catalog(provider, taxonomy: str, level: str) -> pd.DataFrame:
    params = {"level": level}
    if taxonomy == "SW2021":
        params["src"] = taxonomy
    return provider.query("index_classify", **params)


def fetch_catalogs(provider, levels=LEVELS) -> pd.DataFrame:
    """Fetch the SW taxonomy catalogs (six small requests) without downloading members."""
    parts = []
    for taxonomy in ("SW2014", "SW2021"):
        for level in levels:
            parts.append((taxonomy, level, _catalog(provider, taxonomy, level)))
    return normalize_sectors(parts)


def _member_calls(taxonomy: str, level: str, code: str):
    """The exact provider calls and cache keys for one sector."""
    parameter = {"L1": "l1_code", "L2": "l2_code", "L3": "l3_code"}[level]
    if taxonomy == "SW2014":
        return [
            (f"{taxonomy}-{level}-{code}-{state}.parquet", "index_member", {"index_code": code, "is_new": state})
            for state in ("Y", "N")
        ]
    return [
        (
            f"{taxonomy}-{level}-{code}-{state}.parquet",
            "index_member_all",
            {parameter: code, "is_new": state},
        )
        for state in ("Y", "N")
    ]


def _validate_members(frame, taxonomy, level, code):
    if frame is None or frame.empty:
        return
    if len(frame) >= MAX_MEMBER_ROWS:
        raise ValueError("历史行业触及单次上限，拒绝发布截断数据")
    code_field = "index_code" if taxonomy == "SW2014" else {"L1": "l1_code", "L2": "l2_code", "L3": "l3_code"}[level]
    if code_field not in frame or not frame[code_field].astype(str).eq(code).all():
        raise ValueError("历史行业响应分类代码不匹配")


def download_industries(
    *,
    root=None,
    provider=None,
    progress=None,
    refresh=False,
    levels=("L1",),
    pause_file=None,
    cache_key=None,
):
    """Serial, restartable bounded requests for the requested SW levels.

    Defaults to L1 for backward compatibility; the web UI requests all levels.
    A sector response at the vendor row cap is rejected, never published truncated.
    """

    levels = tuple(dict.fromkeys(levels))
    if not levels or not set(levels) <= set(LEVELS):
        raise ValueError("历史行业级别须为 L1、L2 或 L3")
    root = Path(root or industry_root())
    raw_dir = root / "raw"
    if refresh:
        if cache_key is not None and not re.fullmatch(r"[a-f0-9]{24}", cache_key):
            raise ValueError("行业刷新缓存编号无效")
        raw_dir = raw_dir / (cache_key or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f"))
    raw_dir.mkdir(parents=True, exist_ok=True)
    provider = provider or TushareProvider(TushareSettings(TushareSettings.from_env().token))
    progress = progress or (lambda x: None)
    failures = []
    parts = []
    evidence = {}

    def pause():
        if pause_file is not None and Path(pause_file).exists():
            raise DownloadPaused("用户暂停；已完成的行业分区保留，可续传")

    with FileLock(root / "download.lock", timeout=0):
        catalogs = []
        for taxonomy in ("SW2014", "SW2021"):
            for level in levels:
                path = raw_dir / f"{taxonomy}-{level}-catalog.parquet"
                if not path.exists():
                    pause()
                    frame = _catalog(provider, taxonomy, level)
                    _atomic_parquet(frame, path)
                catalogs.append((taxonomy, level, pd.read_parquet(path)))
                progress({"stage": "catalog", "taxonomy": taxonomy, "level": level})
        sectors = normalize_sectors(catalogs)
        sectors["member_status"] = "available"
        for taxonomy in ("SW2014", "SW2021"):
            for level in levels:
                listed = sectors.loc[sectors.classification.eq(taxonomy) & sectors.level.eq(level)]
                for sector in listed.to_dict("records"):
                    code = sector["index_code"]
                    frames = []
                    try:
                        for name, api, params in _member_calls(taxonomy, level, code):
                            path = raw_dir / name
                            if not path.exists():
                                pause()
                                frame = provider.query(api, **params)
                                _validate_members(frame, taxonomy, level, code)
                                _atomic_parquet(frame, path)
                            frame = pd.read_parquet(path)
                            _validate_members(frame, taxonomy, level, code)
                            frames.append(frame)
                            evidence[name] = {"rows": len(frame), "sha256": file_sha256(path)}
                            progress(
                                {
                                    "stage": "members",
                                    "taxonomy": taxonomy,
                                    "level": level,
                                    "code": code,
                                    "rows": len(frame),
                                }
                            )
                        nonempty = [frame for frame in frames if not frame.empty]
                        if not nonempty:
                            if taxonomy != "SW2021" or (level, code) not in EMPTY_A_SHARE_SECTORS:
                                raise ValueError("当前和历史成员均为空，尚未确认是否暂无A股成员，请复核数据源")
                            mask = (
                                sectors.classification.eq(taxonomy)
                                & sectors.level.eq(level)
                                & sectors.index_code.eq(code)
                            )
                            sectors.loc[mask, "member_status"] = "no_a_share_members"
                            progress(
                                {
                                    "stage": "empty_sector",
                                    "taxonomy": taxonomy,
                                    "level": level,
                                    "code": code,
                                    "name": sector["industry_name"],
                                    "reason": "暂无A股成员",
                                }
                            )
                            continue
                        raw = pd.concat(nonempty, ignore_index=True)
                        parts.append((taxonomy, sector, raw))
                    except DownloadPaused:
                        raise
                    except Exception as error:
                        failures.append(
                            {
                                "taxonomy": taxonomy,
                                "level": level,
                                "code": code,
                                "name": sector["industry_name"],
                                "reason": str(error)[:240],
                            }
                        )
                        progress(
                            {
                                "stage": "failure",
                                "taxonomy": taxonomy,
                                "level": level,
                                "code": code,
                                "reason": str(error)[:240],
                                "request_failed": len(frames) < 2,
                            }
                        )
        if failures:
            detail = failures[0]["reason"] if failures else ""
            raise IndustryDownloadIncomplete(
                f"历史行业下载有{len(failures)}个分区失败，未发布新版本；已完成缓存可续传；首个错误：{detail}",
                failures,
            )
        excluded_members = []
        intervals = normalize_level_intervals(parts, excluded_members=excluded_members)
        for member in excluded_members:
            progress({"stage": "excluded_member", **member})
        return publish_industries(
            intervals,
            root=root,
            evidence=evidence,
            data_asof=_today(),
            sectors=sectors,
            excluded_members=excluded_members,
        )
