"""Validated, immutable research configuration with config-relative paths."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field,asdict
from finance_quant.research.neutralization import NeutralizationConfig,parse_neutralization
from finance_quant.research.execution_policy import EXECUTION_FIELDS,ExecutionCosts,validate_frequency
from datetime import date
from numbers import Integral, Real
from pathlib import Path
from typing import Any

from finance_quant.research.registry import (
    EvaluationConfig,
    FactorConfig,
    FactorSpec,
)
from finance_quant.data.validation_config import CrosscheckConfig

__all__ = [
    "DataConfig",
    "BacktestConfig",
    "ResearchConfig",
    "FactorSpec",
    "FactorConfig",
    "EvaluationConfig",
    "load_config",
]

_CODE_PATTERN = re.compile(
    r"^(?:(SH|SZ|BJ)([0-9]{6})|([0-9]{6})\.(SH|SZ|BJ))$",
    re.IGNORECASE,
)
_DATE_PATTERNS = (
    re.compile(r"^([0-9]{4})([0-9]{2})([0-9]{2})$"),
    re.compile(r"^([0-9]{4})-([0-9]{2})-([0-9]{2})$"),
)
_BENCHMARKS = frozenset(
    {"000300.SH", "000905.SH", "000852.SH", "000001.SH", "399001.SZ", "399006.SZ"}
)
_TOP_LEVEL_FIELDS = frozenset({"data", "backtest", "runs_dir", "factors", "evaluation", "neutralization"})
_DATA_FIELDS = frozenset({"root", "start", "end", "codes", "benchmark", "validation"})
_FACTOR_FIELDS = frozenset({"name", "weight", "window", "skip", "baseline_window", "id"})
_EVALUATION_FIELDS = frozenset({"horizons", "groups", "min_cross_section"})
_BACKTEST_FIELDS = frozenset(
    {
        "start",
        "end",
        "initial_cash",
        "topk",
        "n_drop",
        "risk_degree",
        "open_cost",
        "close_cost",
        "min_cost",
    }
)
_BACKTEST_OPTIONALS = (
    "initial_cash",
    "topk",
    "n_drop",
    "risk_degree",
    "open_cost",
    "close_cost",
    "min_cost",
)
_BACKTEST_FIELDS=_BACKTEST_FIELDS.union(EXECUTION_FIELDS)
_BACKTEST_OPTIONALS+=EXECUTION_FIELDS
_BACKTEST_FIELDS=_BACKTEST_FIELDS.union({'benchmark'})
_BACKTEST_OPTIONALS+=('benchmark',)


def _path(value: Any, field: str) -> Path:
    if isinstance(value, Path):
        text = str(value)
    elif isinstance(value, str):
        text = value
    else:
        raise ValueError(f"{field} must be a file system path")
    # Path("") is ".", which cannot be distinguished from an intentional ".";
    # both are rejected here to keep "empty path" unambiguous.
    if not text.strip() or text == ".":
        raise ValueError(f"{field} must not be empty")
    return Path(text)


def _parse_date(value: Any, field: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a date string")
    for pattern in _DATE_PATTERNS:
        match = pattern.fullmatch(value)
        if match is None:
            continue
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            break
    raise ValueError(f"{field} must be a valid date in YYYYMMDD or YYYY-MM-DD form")


def _normalize_code(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must contain stock code strings")
    match = _CODE_PATTERN.fullmatch(value)
    if match is None:
        raise ValueError(
            f"Invalid code in {field}; expected SH600000, 600000.SH, SZ000001, "
            "000001.SZ, BJ430047, or 430047.BJ"
        )
    exchange = match.group(1) or match.group(4)
    digits = match.group(2) or match.group(3)
    return f"{digits}.{exchange.upper()}"


def _normalize_codes(codes: Any) -> tuple[str, ...]:
    if isinstance(codes, (str, bytes)) or not isinstance(codes, (list, tuple)):
        raise ValueError("codes must be a sequence of stock codes")
    normalized = tuple(_normalize_code(code, "codes") for code in codes)
    if len(set(normalized)) != len(normalized):
        raise ValueError("codes must not contain duplicates after normalization")
    if len(normalized) < 2:
        raise ValueError("codes must contain at least two distinct stock codes")
    return normalized


def _finite(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{field} must be a finite number")
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        raise ValueError(f"{field} must be a finite number") from None
    if not math.isfinite(number):
        raise ValueError(f"{field} must be a finite number")
    return number


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise ValueError(f"{field} must be an integer")
    return int(value)


@dataclass(frozen=True)
class DataConfig:
    root: Path
    start: str
    end: str
    codes: tuple[str, ...]
    benchmark: str = "000300.SH"
    validation: CrosscheckConfig = field(default_factory=CrosscheckConfig)

    def __post_init__(self) -> None:
        if not isinstance(self.validation, CrosscheckConfig):
            raise ValueError("data.validation must be a CrosscheckConfig")
        root = _path(self.root, "data.root")
        start_date = _parse_date(self.start, "data.start")
        end_date = _parse_date(self.end, "data.end")
        if start_date > end_date:
            raise ValueError("data.start must not be after data.end")
        codes = _normalize_codes(self.codes)
        benchmark = _normalize_code(self.benchmark, "benchmark")
        if benchmark not in _BENCHMARKS:
            raise ValueError("benchmark is not supported")

        object.__setattr__(self, "root", root)
        object.__setattr__(
            self, "start", f"{start_date.year:04d}{start_date.month:02d}{start_date.day:02d}"
        )
        object.__setattr__(
            self, "end", f"{end_date.year:04d}{end_date.month:02d}{end_date.day:02d}"
        )
        object.__setattr__(self, "codes", codes)
        object.__setattr__(self, "benchmark", benchmark)


@dataclass(frozen=True)
class BacktestConfig:
    start: str
    end: str
    initial_cash: float = 100000.0
    topk: int = 2
    n_drop: int = 1
    risk_degree: float = 0.9
    open_cost: float = 0.001
    close_cost: float = 0.001
    min_cost: float = 5.0
    rebalance_frequency: str = 'daily'
    cost_mode: str = 'legacy'
    commission_rate: float = .0003
    tax_mode: str = 'historical'
    stamp_tax_rate: float = .0005
    transfer_fee_rate: float = .00001
    impact_cost: float = 0.
    benchmark: str | None = None

    def __post_init__(self) -> None:
        start_date = _parse_date(self.start, "backtest.start")
        end_date = _parse_date(self.end, "backtest.end")
        if start_date > end_date:
            raise ValueError("backtest.start must not be after backtest.end")

        cash = _finite(self.initial_cash, "initial_cash")
        if cash <= 0:
            raise ValueError("initial_cash must be greater than zero")

        topk = _integer(self.topk, "topk")
        if topk <= 0:
            raise ValueError("topk must be positive")
        n_drop = _integer(self.n_drop, "n_drop")
        if n_drop < 0:
            raise ValueError("n_drop must be nonnegative")
        if n_drop > topk:
            raise ValueError("n_drop must not exceed topk")

        risk = _finite(self.risk_degree, "risk_degree")
        if not 0 < risk <= 1:
            raise ValueError("risk_degree must be in (0, 1]")

        open_cost = _finite(self.open_cost, "open_cost")
        close_cost = _finite(self.close_cost, "close_cost")
        if not 0 <= open_cost < 1:
            raise ValueError("open_cost must be in [0, 1)")
        if not 0 <= close_cost < 1:
            raise ValueError("close_cost must be in [0, 1)")
        min_cost = _finite(self.min_cost, "min_cost")
        if min_cost < 0:
            raise ValueError("min_cost must be nonnegative")
        validate_frequency(self.rebalance_frequency)
        costs=ExecutionCosts(**{key:getattr(self,key) for key in ExecutionCosts.__dataclass_fields__})
        costs.validate_range(start_date.isoformat())
        if self.benchmark is not None:
            from finance_quant.data.benchmarks import normalize_benchmark
            object.__setattr__(self,'benchmark',normalize_benchmark(self.benchmark))
        for key in ('commission_rate','stamp_tax_rate','transfer_fee_rate','impact_cost'):
            object.__setattr__(self,key,getattr(costs,key))

        object.__setattr__(self, "start", start_date.isoformat())
        object.__setattr__(self, "end", end_date.isoformat())
        object.__setattr__(self, "initial_cash", cash)
        object.__setattr__(self, "topk", topk)
        object.__setattr__(self, "n_drop", n_drop)
        object.__setattr__(self, "risk_degree", risk)
        object.__setattr__(self, "open_cost", open_cost)
        object.__setattr__(self, "close_cost", close_cost)
        object.__setattr__(self, "min_cost", min_cost)


@dataclass(frozen=True)
class ResearchConfig:
    data: DataConfig
    backtest: BacktestConfig
    runs_dir: Path
    factors: FactorConfig = field(default_factory=FactorConfig)
    evaluation: EvaluationConfig = field(default_factory=EvaluationConfig)
    neutralization: NeutralizationConfig = field(default_factory=NeutralizationConfig)

    def __post_init__(self) -> None:
        if not isinstance(self.data, DataConfig):
            raise ValueError("data must be a DataConfig")
        if not isinstance(self.backtest, BacktestConfig):
            raise ValueError("backtest must be a BacktestConfig")
        if not isinstance(self.factors, FactorConfig):
            raise ValueError("factors must be a FactorConfig")
        if not isinstance(self.evaluation, EvaluationConfig):
            raise ValueError("evaluation must be an EvaluationConfig")
        if not isinstance(self.neutralization,NeutralizationConfig):raise ValueError("neutralization must be a NeutralizationConfig")
        runs_dir = _path(self.runs_dir, "runs_dir")

        data_start = _parse_date(self.data.start, "data.start")
        data_end = _parse_date(self.data.end, "data.end")
        backtest_start = _parse_date(self.backtest.start, "backtest.start")
        backtest_end = _parse_date(self.backtest.end, "backtest.end")
        if backtest_start < data_start or backtest_end > data_end:
            raise ValueError("backtest range must be inside the data range")
        if self.backtest.topk > len(self.data.codes):
            raise ValueError("topk must not exceed the number of data codes")

        object.__setattr__(self, "runs_dir", runs_dir)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable canonical representation."""
        return {
            "data": {
                "root": str(self.data.root),
                "start": self.data.start,
                "end": self.data.end,
                "codes": list(self.data.codes),
                "benchmark": self.data.benchmark,
                "validation": self.data.validation.as_dict(),
            },
            "backtest": asdict(self.backtest),
            "runs_dir": str(self.runs_dir),
            "factors": self.factors.as_list(),
            "evaluation": self.evaluation.as_dict(),
            **({"neutralization":self.neutralization.as_dict()} if self.neutralization != NeutralizationConfig() else {}),
        }


def _reject_unknown(obj: dict[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = sorted(set(obj) - allowed)
    if unknown:
        raise ValueError(f"Unknown field '{unknown[0]}' in {label}")


def _require_fields(obj: dict[str, Any], fields: tuple[str, ...], label: str) -> None:
    for field in fields:
        if field not in obj:
            raise ValueError(f"Missing required field '{field}' in {label}")


def _json_object(obj: dict[str, Any], field: str) -> dict[str, Any]:
    value = obj[field]
    if not isinstance(value, dict):
        raise ValueError(f"'{field}' must be a JSON object")
    return value


def _resolve_path(value: Any, base: Path, field: str) -> Path:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a path string")
    if not value.strip():
        raise ValueError(f"{field} must not be empty")
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve()


def _factor_config(document: dict[str, Any]) -> FactorConfig:
    if "factors" not in document:
        return FactorConfig()
    value = document["factors"]
    if not isinstance(value, list):
        raise ValueError("'factors' must be a JSON array")
    specs = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("each factor must be a JSON object")
        _reject_unknown(item, _FACTOR_FIELDS, "factor configuration")
        _require_fields(item, ("name", "weight"), "factor configuration")
        specs.append(FactorSpec(**item))
    return FactorConfig(tuple(specs))


def _evaluation_config(document: dict[str, Any]) -> EvaluationConfig:
    if "evaluation" not in document:
        return EvaluationConfig()
    value = document["evaluation"]
    if not isinstance(value, dict):
        raise ValueError("'evaluation' must be a JSON object")
    _reject_unknown(value, _EVALUATION_FIELDS, "evaluation configuration")
    return EvaluationConfig(
        **{key: value[key] for key in _EVALUATION_FIELDS if key in value}
    )


def load_config(path: Path) -> ResearchConfig:
    """Load JSON without evaluation; relative paths are based at the config file."""
    try:
        config_path = Path(path)
    except TypeError:
        raise ValueError("path must be a file system path") from None

    try:
        text = config_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        raise ValueError("Unable to read configuration file") from None

    try:
        def unique_fields(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Configuration contains duplicate fields")
                result[key] = value
            return result
        document = json.loads(text, object_pairs_hook=unique_fields)
    except json.JSONDecodeError:
        raise ValueError("Configuration file contains malformed JSON") from None
    if not isinstance(document, dict):
        raise ValueError("Configuration must be a JSON object")

    _reject_unknown(document, _TOP_LEVEL_FIELDS, "configuration")
    _require_fields(document, ("data", "backtest"), "configuration")

    data_obj = _json_object(document, "data")
    _reject_unknown(data_obj, _DATA_FIELDS, "data configuration")
    _require_fields(data_obj, ("root", "start", "end", "codes"), "data configuration")
    if not isinstance(data_obj["codes"], list):
        raise ValueError("'data.codes' must be a JSON array")

    backtest_obj = _json_object(document, "backtest")
    _reject_unknown(backtest_obj, _BACKTEST_FIELDS, "backtest configuration")
    _require_fields(backtest_obj, ("start", "end"), "backtest configuration")

    base = config_path.parent.resolve()
    data = DataConfig(
        root=_resolve_path(data_obj["root"], base, "data.root"),
        start=data_obj["start"],
        end=data_obj["end"],
        codes=data_obj["codes"],
        benchmark=data_obj.get("benchmark", "000300.SH"),
        validation=CrosscheckConfig.from_dict(data_obj.get("validation", {})),
    )
    backtest = BacktestConfig(
        start=backtest_obj["start"],
        end=backtest_obj["end"],
        **{key: backtest_obj[key] for key in _BACKTEST_OPTIONALS if key in backtest_obj},
    )
    runs_dir = (
        _resolve_path(document["runs_dir"], base, "runs_dir")
        if "runs_dir" in document
        else (base / "runs").resolve()
    )
    return ResearchConfig(
        data=data,
        backtest=backtest,
        runs_dir=runs_dir,
        factors=_factor_config(document),
        evaluation=_evaluation_config(document),
        neutralization=parse_neutralization(document.get("neutralization")),
    )
