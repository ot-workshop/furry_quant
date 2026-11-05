"""Bounded, cached adapter for BaoStock daily history.

The BaoStock SDK keeps global socket/session state and can block forever while
reading from a closed socket.  The adapter therefore never imports the SDK in
the parent process: every fetch runs in a short-lived worker subprocess with a
hard timeout.  Successful responses are published atomically to an immutable,
checksum-verified cache entry.
"""

from __future__ import annotations

import importlib.metadata
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from filelock import FileLock

from finance_quant.checksums import file_sha256, sha256_bytes

SCHEMA = "baostock-history-v1"
FIELDS = (
    "date",
    "code",
    "open",
    "high",
    "low",
    "close",
    "preclose",
    "volume",
    "amount",
    "adjustflag",
    "pctChg",
    "tradestatus",
    "isST",
)

_CODE_RE = re.compile(r"^\d{6}\.(?:SH|SZ)$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_COMPACT_DATE_RE = re.compile(r"^\d{8}$")
_WORKER_ERROR = "BaoStock worker failed"
_WORKER_INVALID = "BaoStock worker returned invalid data"
_CACHE_CORRUPT = "BaoStock cache entry is corrupted"


class BaoStockProviderError(RuntimeError):
    """Public adapter error; never carries an external response body or traceback."""

    def __init__(self, message, *, retryable=False):
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class BaoStockBundle:
    """Original daily bars plus the deterministic cache metadata."""

    raw: pd.DataFrame
    adjusted: pd.DataFrame
    metadata: dict


def _normalize_date(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise BaoStockProviderError(f"{label} must be a YYYYMMDD or YYYY-MM-DD date")
    try:
        if _COMPACT_DATE_RE.fullmatch(value):
            parsed = datetime.strptime(value, "%Y%m%d").date()
        elif _ISO_DATE_RE.fullmatch(value):
            parsed = datetime.strptime(value, "%Y-%m-%d").date()
        else:
            raise ValueError
    except ValueError:
        raise BaoStockProviderError(f"{label} must be a YYYYMMDD or YYYY-MM-DD date") from None
    return parsed.isoformat()


def _validate_code(code: str) -> str:
    if not isinstance(code, str) or _CODE_RE.fullmatch(code) is None:
        raise BaoStockProviderError("code must be a canonical six-digit Shanghai or Shenzhen instrument")
    return code


def _sdk_version() -> str:
    try:
        version = importlib.metadata.version("baostock")
    except importlib.metadata.PackageNotFoundError:
        raise BaoStockProviderError("BaoStock SDK is not installed") from None
    except Exception:
        raise BaoStockProviderError("BaoStock SDK version is unavailable") from None
    if not isinstance(version, str) or not version.strip():
        raise BaoStockProviderError("BaoStock SDK version is unavailable")
    return version.strip()


def _cache_key(request: dict, fields: str, sdk_version: str, namespace: str) -> str:
    spec = {
        "schema": SCHEMA,
        "request": request,
        "fields": fields,
        "sdk_version": sdk_version,
        "namespace": namespace,
    }
    encoded = json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return sha256_bytes(encoded)


_sha256 = file_sha256


def _guard(root: Path, path: Path) -> Path:
    try:
        resolved = path.resolve()
        resolved.relative_to(root)
    except (OSError, ValueError, RuntimeError):
        raise BaoStockProviderError("BaoStock cache path is unsafe") from None
    return resolved


def _write_temp(parent: Path, writer) -> Path:
    handle = tempfile.NamedTemporaryFile(mode="wb", prefix=".tmp-", dir=str(parent), delete=False)
    temporary = Path(handle.name)
    try:
        handle.close()
        writer(temporary)
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    return temporary


def _validate_rows(value) -> pd.DataFrame:
    if not isinstance(value, list) or not value:
        raise BaoStockProviderError(_WORKER_INVALID)
    if not all(isinstance(row, dict) for row in value):
        raise BaoStockProviderError(_WORKER_INVALID)
    if not all(all(field in row for field in FIELDS) for row in value):
        raise BaoStockProviderError(_WORKER_INVALID)
    try:
        frame = pd.DataFrame(value)
    except Exception:
        raise BaoStockProviderError(_WORKER_INVALID) from None
    if not set(FIELDS).issubset(frame.columns):
        raise BaoStockProviderError(_WORKER_INVALID)
    return frame.reindex(columns=list(FIELDS))


def _validate_payload(payload) -> tuple[pd.DataFrame, pd.DataFrame]:
    if not isinstance(payload, dict) or set(payload) != {"raw", "adjusted"}:
        raise BaoStockProviderError(_WORKER_INVALID)
    return _validate_rows(payload["raw"]), _validate_rows(payload["adjusted"])


class BaoStockProvider:
    """Fetch BaoStock daily history through an isolated worker with a file cache."""

    def __init__(
        self,
        cache_root: Path,
        namespace: str = "base",
        timeout: float = 45,
        worker=None,
        adjusted_flag: str = "1",
    ) -> None:
        if not isinstance(namespace, str) or not namespace.strip():
            raise BaoStockProviderError("namespace must be a non-empty string")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or float(timeout) <= 0
        ):
            raise BaoStockProviderError("timeout must be a positive number")
        if worker is not None and not callable(worker):
            raise BaoStockProviderError("worker must be callable")
        if adjusted_flag not in {"1", "2"}:
            raise BaoStockProviderError("adjusted_flag must be 1 or 2")
        self.adjusted_flag = adjusted_flag
        self.cache_root = Path(cache_root)
        self.namespace = namespace
        self.timeout = float(timeout)
        self._worker = worker

    def fetch(self, code: str, start: str, end: str) -> BaoStockBundle:
        """Return raw (adjustflag=3) and back-adjusted (adjustflag=1) bars."""
        code = _validate_code(code)
        start_iso = _normalize_date(start, "start")
        end_iso = _normalize_date(end, "end")
        if start_iso > end_iso:
            raise BaoStockProviderError("start date must not be after end date")

        request = {"code": code, "start": start_iso, "end": end_iso}
        if self.adjusted_flag != "1":
            request["adjusted_flag"] = self.adjusted_flag
        version = _sdk_version()
        key = _cache_key(request, ",".join(FIELDS), version, self.namespace)

        root = self.cache_root.resolve()
        base = _guard(root, self.cache_root / "baostock")
        keydir = _guard(root, base / key)
        lock_path = _guard(root, base / f"{key}.lock")

        try:
            base.mkdir(parents=True, exist_ok=True)
            with FileLock(str(lock_path), timeout=0):
                if keydir.exists():
                    return self._load_cached(root, keydir, key, request, version)
                return self._fetch_and_publish(root, base, keydir, key, request, version)
        except BaoStockProviderError:
            raise
        except Exception:
            from finance_quant.data.network_budget import DownloadPaused

            if isinstance(sys.exception(), DownloadPaused):
                raise
            raise BaoStockProviderError("BaoStock cache is unavailable") from None

    # -- cache -----------------------------------------------------------------

    def alternate_basis(self):
        return BaoStockProvider(
            self.cache_root,
            namespace=self.namespace,
            timeout=self.timeout,
            worker=self._worker,
            adjusted_flag="2" if self.adjusted_flag == "1" else "1",
        )

    def _fetch_and_publish(
        self,
        root: Path,
        base: Path,
        keydir: Path,
        key: str,
        request: dict,
        version: str,
    ) -> BaoStockBundle:
        # A fresh subprocess opens a fresh SDK connection. Retry transport/worker
        # failures once; invalid payloads and corrupt caches remain hard errors.
        # Shared across cache roots, stocks, threads and our local processes.
        # A timed-out worker has exited before another connection can start.
        with FileLock(Path(tempfile.gettempdir()) / "finance-quant-baostock.network.lock", timeout=120):
            for attempt in range(2):
                try:
                    payload = self._invoke_worker(dict(request))
                    break
                except BaoStockProviderError as error:
                    if not error.retryable or attempt:
                        raise
                    print(f"baostock {request['code']}: reconnecting once", flush=True)
        raw, adjusted = _validate_payload(payload)

        staging = _guard(root, base / f".{key}.{uuid.uuid4().hex}.tmp")
        try:
            staging.mkdir()
            raw_path = _guard(root, staging / "raw.parquet")
            adjusted_path = _guard(root, staging / "adjusted.parquet")
            metadata_path = _guard(root, staging / "metadata.json")

            _publish_parquet(raw, raw_path)
            _publish_parquet(adjusted, adjusted_path)

            files = {
                "raw.parquet": _sha256(raw_path),
                "adjusted.parquet": _sha256(adjusted_path),
            }
            fetched_at = datetime.now(timezone.utc).isoformat()
            metadata = self._metadata(
                request=request,
                version=version,
                key=key,
                cache_path=_relative_cache_path(root, keydir),
                files=files,
                fetched_at=fetched_at,
            )
            _publish_json(metadata, metadata_path)
            os.replace(staging, keydir)
        except BaoStockProviderError:
            raise
        except Exception:
            raise BaoStockProviderError("failed to publish the BaoStock cache entry") from None
        finally:
            if staging.exists():
                try:
                    for child in staging.iterdir():
                        child.unlink()
                    staging.rmdir()
                except OSError:
                    pass

        return BaoStockBundle(raw=raw, adjusted=adjusted, metadata=metadata)

    def _load_cached(
        self,
        root: Path,
        keydir: Path,
        key: str,
        request: dict,
        version: str,
    ) -> BaoStockBundle:
        metadata_path = _guard(root, keydir / "metadata.json")
        raw_path = _guard(root, keydir / "raw.parquet")
        adjusted_path = _guard(root, keydir / "adjusted.parquet")
        try:
            if not metadata_path.is_file() or not raw_path.is_file() or not adjusted_path.is_file():
                raise BaoStockProviderError(_CACHE_CORRUPT)
            stored = json.loads(metadata_path.read_text(encoding="utf-8"))
            if not isinstance(stored, dict):
                raise BaoStockProviderError(_CACHE_CORRUPT)

            files = stored.get("files")
            if (
                stored.get("source") != "baostock"
                or stored.get("schema") != SCHEMA
                or stored.get("request") != request
                or stored.get("sdk_version") != version
                or stored.get("cache_key") != key
                or stored.get("cache_path") != _relative_cache_path(root, keydir)
                or not isinstance(stored.get("fetched_at"), str)
                or not stored.get("fetched_at")
                or not isinstance(files, dict)
                or set(files) != {"raw.parquet", "adjusted.parquet"}
            ):
                raise BaoStockProviderError(_CACHE_CORRUPT)

            raw_hash = _sha256(raw_path)
            adjusted_hash = _sha256(adjusted_path)
            if files["raw.parquet"] != raw_hash or files["adjusted.parquet"] != adjusted_hash:
                raise BaoStockProviderError(_CACHE_CORRUPT)

            raw = pd.read_parquet(raw_path)
            adjusted = pd.read_parquet(adjusted_path)
            if (
                not isinstance(raw, pd.DataFrame)
                or not isinstance(adjusted, pd.DataFrame)
                or not set(FIELDS).issubset(raw.columns)
                or not set(FIELDS).issubset(adjusted.columns)
            ):
                raise BaoStockProviderError(_CACHE_CORRUPT)
            raw = raw.reindex(columns=list(FIELDS))
            adjusted = adjusted.reindex(columns=list(FIELDS))
        except BaoStockProviderError:
            raise
        except Exception:
            raise BaoStockProviderError(_CACHE_CORRUPT) from None

        metadata = self._metadata(
            request=request,
            version=version,
            key=key,
            cache_path=_relative_cache_path(root, keydir),
            files={"raw.parquet": raw_hash, "adjusted.parquet": adjusted_hash},
            fetched_at=stored["fetched_at"],
        )
        return BaoStockBundle(raw=raw, adjusted=adjusted, metadata=metadata)

    def _metadata(
        self,
        *,
        request: dict,
        version: str,
        key: str,
        cache_path: str,
        files: dict,
        fetched_at: str,
    ) -> dict:
        return {
            "source": "baostock",
            **({"adjusted_flag": request["adjusted_flag"]} if "adjusted_flag" in request else {}),
            "schema": SCHEMA,
            "request": request,
            "sdk_version": version,
            "fetched_at": fetched_at,
            "cache_key": key,
            "cache_path": cache_path,
            "files": {
                "raw.parquet": files["raw.parquet"],
                "adjusted.parquet": files["adjusted.parquet"],
            },
        }

    # -- worker ----------------------------------------------------------------

    def _invoke_worker(self, request: dict):
        if self._worker is None:
            return self._run_subprocess_worker(request)
        try:
            return self._worker(request)
        except TimeoutError:
            raise BaoStockProviderError("BaoStock worker timed out", retryable=True) from None
        except Exception:
            from finance_quant.data.network_budget import DownloadPaused

            if isinstance(sys.exception(), DownloadPaused):
                raise
            raise BaoStockProviderError(_WORKER_ERROR, retryable=True) from None

    def _run_subprocess_worker(self, request: dict):
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "finance_quant.data.providers.baostock_worker"],
                input=json.dumps(request),
                text=True,
                capture_output=True,
                timeout=self.timeout,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            raise BaoStockProviderError("BaoStock worker timed out", retryable=True) from None
        except Exception:
            raise BaoStockProviderError(_WORKER_ERROR, retryable=True) from None

        if completed.returncode == 3:
            from finance_quant.data.network_budget import DownloadPaused

            raise DownloadPaused("BaoStock达到预算或限制访问，已保留断点")
        if completed.returncode != 0:
            raise BaoStockProviderError(_WORKER_ERROR, retryable=True)
        try:
            return json.loads(completed.stdout)
        except Exception:
            raise BaoStockProviderError(_WORKER_INVALID) from None


def _relative_cache_path(root: Path, keydir: Path) -> str:
    return keydir.resolve().relative_to(root).as_posix()


def _publish_parquet(frame: pd.DataFrame, destination: Path) -> None:
    temporary = _write_temp(destination.parent, lambda path: frame.to_parquet(path, index=False))
    try:
        os.replace(temporary, destination)
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _publish_json(value: dict, destination: Path) -> None:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    temporary = _write_temp(destination.parent, lambda path: path.write_text(text, encoding="utf-8"))
    try:
        os.replace(temporary, destination)
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


__all__ = [
    "BaoStockBundle",
    "BaoStockProvider",
    "BaoStockProviderError",
    "FIELDS",
    "SCHEMA",
]
