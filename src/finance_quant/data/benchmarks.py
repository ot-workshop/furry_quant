"""Independent, immutable benchmark observations; never used as stock signals."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from filelock import FileLock

from finance_quant.checksums import file_sha256, sha256_text
from finance_quant.data.managed import atomic_json
from finance_quant.data.providers.tushare import TushareProvider, TushareProviderError, TushareSettings

BENCHMARKS = {
    "000300.SH": ("沪深300价格指数", "price"),
    "H00300.CSI": ("沪深300全收益指数", "gross_total_return"),
    "000905.SH": ("中证500价格指数", "price"),
    "000852.SH": ("中证1000价格指数", "price"),
    "000001.SH": ("上证价格指数", "price"),
    "399001.SZ": ("深证成指价格指数", "price"),
    "399006.SZ": ("创业板价格指数", "price"),
}
VERSION = "official-benchmark-close-v1"


def normalize_benchmark(code):
    if not isinstance(code, str) or code.upper() not in BENCHMARKS:
        raise ValueError("不支持的评价基准，请从基准列表选择")
    return code.upper()


def benchmark_root():
    configured = os.environ.get("FINANCE_QUANT_BENCHMARK_ROOT")
    if configured:
        return Path(configured)
    from finance_quant.paths import data_root

    return data_root() / "benchmarks/official-tushare"


def normalize_series(raw, code):
    code = normalize_benchmark(code)
    if (
        not isinstance(raw, pd.DataFrame)
        or raw.empty
        or not {"ts_code", "trade_date", "close", "pre_close"} <= set(raw)
    ):
        raise ValueError("基准日线为空或缺少必要字段")
    if not raw.ts_code.eq(code).all():
        raise ValueError("基准响应代码不匹配")
    dates = pd.to_datetime(raw.trade_date.astype(str), format="%Y%m%d", errors="raise")
    frame = (
        pd.DataFrame(
            {
                "date": dates,
                "close": pd.to_numeric(raw.close, errors="raise"),
                "pre_close": pd.to_numeric(raw.pre_close, errors="raise"),
            }
        )
        .sort_values("date")
        .reset_index(drop=True)
    )
    # The official base observation has no preceding return/OHLC. Keep its
    # actual 1000-point close as an anchor; never invent a previous close.
    base = (
        (code == "H00300.CSI")
        & frame.date.eq(pd.Timestamp("2004-12-31"))
        & frame.close.eq(1000.0)
        & frame.pre_close.isna()
    )
    preceding = frame.loc[~base, "pre_close"]
    if (
        frame.date.duplicated().any()
        or not np.isfinite(frame.close).all()
        or frame.close.le(0).any()
        or not np.isfinite(preceding).all()
        or preceding.le(0).any()
    ):
        raise ValueError("基准包含重复日期、非有限值或非正点位")
    if len(frame) > 1 and not np.allclose(frame.pre_close.iloc[1:], frame.close.iloc[:-1], rtol=2e-6, atol=0.001):
        raise ValueError("基准前收盘点位不连续，请复核缺口或历史修订")
    return frame


def validate_coverage(frame, required):
    required = pd.DatetimeIndex(required)
    if required.empty or required.has_duplicates or not required.is_monotonic_increasing:
        raise ValueError("基准校验需要有序、唯一且非空的交易日历")
    observed = pd.DatetimeIndex(frame.date)
    missing = required.difference(observed)
    if len(missing):
        raise ValueError(f"基准缺少{len(missing)}个交易日（含期初锚点），如{missing[0]:%Y-%m-%d}；请补齐后重试")
    selected = frame.loc[frame.date.between(required[0], required[-1])]
    if not pd.DatetimeIndex(selected.date).equals(required):
        raise ValueError("基准日期与账户交易日历不一致")
    return selected.reset_index(drop=True)


def _digest(frame):
    return sha256_text(frame.to_csv(index=False, date_format="%Y-%m-%d", float_format="%.12g"))


def load_series(code, root=None):
    code = normalize_benchmark(code)
    directory = (Path(root) if root is not None else benchmark_root()) / code
    pointer = directory / "manifest.json"
    manifest = json.loads(pointer.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != VERSION or manifest.get("code") != code:
        raise ValueError("基准缓存版本或代码无效")
    path = (directory / manifest["file"]).resolve()
    if not path.is_relative_to(directory.resolve()) or file_sha256(path) != manifest["sha256"]:
        raise ValueError("基准缓存文件校验失败")
    frame = pd.read_parquet(path)
    if _digest(frame) != manifest["series_sha256"]:
        raise ValueError("基准序列内容校验失败")
    return frame, manifest


def publish_series(code, frame, *, root=None):
    code = normalize_benchmark(code)
    directory = (Path(root) if root is not None else benchmark_root()) / code
    directory.mkdir(parents=True, exist_ok=True)
    # Validate even locally imported official responses before publication.
    frame = normalize_series(
        pd.DataFrame(
            {
                "ts_code": code,
                "trade_date": frame.date.dt.strftime("%Y%m%d"),
                "close": frame.close,
                "pre_close": frame.pre_close,
            }
        ),
        code,
    )
    digest = _digest(frame)
    relative = f"versions/{digest}/series.parquet"
    path = directory / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        temporary = path.with_suffix(".tmp.parquet")
        frame.to_parquet(temporary, index=False)
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
        finally:
            temporary.unlink(missing_ok=True)
    metadata = {
        "schema_version": VERSION,
        "code": code,
        "name": BENCHMARKS[code][0],
        "return_type": BENCHMARKS[code][1],
        "source": "official_tushare",
        "endpoint": "https://api.tushare.pro",
        "api": "index_daily",
        "start": frame.date.min().strftime("%Y-%m-%d"),
        "end": frame.date.max().strftime("%Y-%m-%d"),
        "rows": len(frame),
        "file": relative,
        "sha256": file_sha256(path),
        "series_sha256": digest,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }
    version = path.with_name("manifest.json")
    if version.exists():
        metadata = json.loads(version.read_text(encoding="utf-8"))
    else:
        atomic_json(version, metadata)
    atomic_json(directory / "manifest.json", metadata)
    return frame, metadata


def ensure_series(code, required, *, root=None, provider=None):
    """Reuse verified coverage; fetch missing years serially from official only."""
    code = normalize_benchmark(code)
    root = Path(root) if root is not None else benchmark_root()
    sessions = pd.DatetimeIndex(required)
    if sessions.empty or sessions.has_duplicates or not sessions.is_monotonic_increasing:
        raise ValueError("基准校验需要有序、唯一且非空的交易日历")
    root.mkdir(parents=True, exist_ok=True)
    with FileLock(root / f"{code}.lock", timeout=30):
        try:
            frame, metadata = load_series(code, root)
        except FileNotFoundError:
            frame = None
        if frame is not None:
            try:
                return validate_coverage(frame, required), metadata
            except ValueError:
                if sessions.difference(pd.DatetimeIndex(frame.date)).empty:
                    raise
        if provider is None:
            try:
                provider = TushareProvider(TushareSettings(TushareSettings.from_env().token))
            except TushareProviderError:
                raise ValueError("官方Tushare基准凭据未配置，无法补齐所选基准") from None
        missing = sessions if frame is None else sessions.difference(pd.DatetimeIndex(frame.date))
        parts = []
        try:
            for year in sorted(set(missing.year)):
                selected = sessions[sessions.year == year]
                left, right = selected[0], selected[-1]
                raw = provider.query(
                    "index_daily", ts_code=code, start_date=left.strftime("%Y%m%d"), end_date=right.strftime("%Y%m%d")
                )
                parts.append(normalize_series(raw, code))
        except TushareProviderError as error:
            raise ValueError(f"官方Tushare无法获取所选基准（{error.category}），未替换为其他口径") from None
        fetched = pd.concat(parts, ignore_index=True).sort_values("date").reset_index(drop=True)
        merged = (
            fetched
            if frame is None
            else pd.concat([frame, fetched])
            .drop_duplicates("date", keep="last")
            .sort_values("date")
            .reset_index(drop=True)
        )
        validate_coverage(merged, sessions)
        saved, metadata = publish_series(code, merged, root=root)
        return validate_coverage(saved, sessions), metadata


def benchmark_options():
    items = []
    for code, (name, kind) in BENCHMARKS.items():
        item = {"code": code, "name": name, "return_type": kind}
        try:
            _, manifest = load_series(code)
            item["coverage"] = {k: manifest[k] for k in ("start", "end", "rows", "source")}
        except (OSError, ValueError, KeyError):
            pass
        items.append(item)
    return items
