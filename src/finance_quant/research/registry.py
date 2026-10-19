"""Validated, immutable factor selection and evaluation configuration."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from numbers import Integral, Real
from typing import Any

__all__ = [
    "FactorSpec",
    "FactorConfig",
    "EvaluationConfig",
    "FACTOR_NAMES",
    "FACTOR_DIRECTIONS",
    "FACTOR_DISPLAY_NAMES",
    "DEFAULT_WINDOWS",
]

FACTOR_NAMES = ("momentum", "value", "volatility")
# Causal daily factors only; financial-statement factors remain out of scope.
FACTOR_DIRECTIONS = {"momentum": "higher", "value": "higher", "volatility": "lower"}
FACTOR_DISPLAY_NAMES = {
    "momentum": "动量",
    "value": "盈利收益率",
    "volatility": "波动率",
}
DEFAULT_WINDOWS = {"momentum": 20, "value": None, "volatility": 10}


def _weight(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError("factor weight must be a finite number in (0, 1]")
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        raise ValueError("factor weight must be a finite number in (0, 1]") from None
    if not math.isfinite(number) or not 0 < number <= 1:
        raise ValueError("factor weight must be a finite number in (0, 1]")
    return number


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise ValueError(f"{field} must be an integer")
    return int(value)


@dataclass(frozen=True)
class FactorSpec:
    name: str
    weight: float
    window: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or self.name not in FACTOR_NAMES:
            raise ValueError("unsupported factor name; choose momentum, value or volatility")
        weight = _weight(self.weight)
        if self.name == "value":
            if self.window is not None:
                raise ValueError("value factor does not accept a window")
            window = None
        else:
            if self.window is None:
                window = DEFAULT_WINDOWS[self.name]
            else:
                window = _integer(self.window, "factor window")
                if not 2 <= window <= 252:
                    raise ValueError("factor window must be between 2 and 252")
        object.__setattr__(self, "weight", weight)
        object.__setattr__(self, "window", window)


@dataclass(frozen=True)
class FactorConfig:
    specs: tuple[FactorSpec, ...] = (
        FactorSpec("momentum", 0.4, 20),
        FactorSpec("value", 0.3),
        FactorSpec("volatility", 0.3, 10),
    )

    def __post_init__(self) -> None:
        if isinstance(self.specs, (str, bytes)) or not isinstance(
            self.specs, (tuple, list)
        ):
            raise ValueError("factor specs must be a sequence of FactorSpec")
        if not self.specs:
            raise ValueError("at least one factor must be selected")
        specs = []
        for spec in self.specs:
            if not isinstance(spec, FactorSpec):
                raise ValueError("factor specs must be FactorSpec instances")
            specs.append(spec)
        names = [spec.name for spec in specs]
        if len(set(names)) != len(names):
            raise ValueError("factor names must be unique")
        if abs(sum(spec.weight for spec in specs) - 1.0) > 1e-9:
            raise ValueError("factor weights must sum to 1")
        object.__setattr__(self, "specs", tuple(sorted(specs, key=lambda spec: spec.name)))

    @property
    def weights(self) -> dict[str, float]:
        return {spec.name: spec.weight for spec in self.specs}

    def as_list(self) -> list[dict[str, Any]]:
        return [
            {"name": spec.name, "weight": spec.weight, "window": spec.window}
            for spec in self.specs
        ]

    @property
    def scheme(self) -> str:
        payload = json.dumps(self.as_list(), sort_keys=True)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
        return f"market-{digest}"


@dataclass(frozen=True)
class EvaluationConfig:
    horizons: tuple[int, ...] = (1, 5, 10)
    groups: int = 3
    min_cross_section: int = 3

    def __post_init__(self) -> None:
        if isinstance(self.horizons, (str, bytes)) or not isinstance(
            self.horizons, (tuple, list)
        ):
            raise ValueError("horizons must be a sequence of integers")
        if not self.horizons:
            raise ValueError("horizons must not be empty")
        horizons = []
        for value in self.horizons:
            horizon = _integer(value, "horizon")
            if not 1 <= horizon <= 252:
                raise ValueError("horizons must be between 1 and 252")
            horizons.append(horizon)
        if len(set(horizons)) != len(horizons):
            raise ValueError("horizons must be unique")

        groups = _integer(self.groups, "groups")
        if not 2 <= groups <= 10:
            raise ValueError("groups must be between 2 and 10")
        min_cross_section = _integer(self.min_cross_section, "min_cross_section")
        if not 3 <= min_cross_section <= 100000:
            raise ValueError("min_cross_section must be between 3 and 100000")
        if min_cross_section < groups:
            raise ValueError("min_cross_section must be at least groups")

        object.__setattr__(self, "horizons", tuple(sorted(horizons)))
        object.__setattr__(self, "groups", groups)
        object.__setattr__(self, "min_cross_section", min_cross_section)

    def as_dict(self) -> dict[str, Any]:
        return {
            "horizons": list(self.horizons),
            "groups": self.groups,
            "min_cross_section": self.min_cross_section,
        }
