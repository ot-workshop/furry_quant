"""Validated, immutable factor selection and evaluation configuration."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from numbers import Integral, Real
from typing import Any

from finance_quant.checksums import sha256_text
from finance_quant.research.factor_library import FACTOR_LIBRARY, FILLABLE_FACTORS

__all__ = [
    "FactorSpec",
    "FactorConfig",
    "EvaluationConfig",
    "FACTOR_NAMES",
    "FACTOR_DIRECTIONS",
    "FACTOR_DISPLAY_NAMES",
    "DEFAULT_WINDOWS",
]

FACTOR_NAMES = tuple(FACTOR_LIBRARY)
FACTOR_DIRECTIONS = {name: definition.direction for name, definition in FACTOR_LIBRARY.items()}
FACTOR_DISPLAY_NAMES = {name: definition.label for name, definition in FACTOR_LIBRARY.items()}
DEFAULT_WINDOWS = {name: definition.default_window for name, definition in FACTOR_LIBRARY.items()}


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
    skip: int | None = None
    baseline_window: int | None = None
    id: str | None = None
    missing_policy: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or self.name not in FACTOR_NAMES:
            raise ValueError("unsupported factor name; choose a registered daily factor")
        definition = FACTOR_LIBRARY[self.name]
        policy = (
            self.missing_policy
            if self.missing_policy is not None
            else ("zero" if self.name in FILLABLE_FACTORS else "exclude")
        )
        if not isinstance(policy, str) or policy not in (
            {"zero", "neutral", "exclude"} if self.name in FILLABLE_FACTORS else {"exclude"}
        ):
            raise ValueError(
                "missing_policy must be zero, neutral or exclude for earnings yield/gross margin; other factors require exclude"
            )
        object.__setattr__(self, "missing_policy", policy)
        weight = _weight(self.weight)
        if definition.default_window is None:
            if self.window is not None:
                raise ValueError(f"{self.name} factor does not accept a window")
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
        skip = definition.default_skip if self.skip is None else self.skip
        if definition.default_skip is None:
            if self.skip is not None:
                raise ValueError(f"{self.name} does not accept skip")
        else:
            skip = _integer(skip, "skip")
            if not 0 <= skip < window:
                raise ValueError("skip must be nonnegative and smaller than window")
        baseline = definition.default_baseline_window if self.baseline_window is None else self.baseline_window
        if definition.default_baseline_window is None:
            if self.baseline_window is not None:
                raise ValueError(f"{self.name} does not accept baseline_window")
        else:
            baseline = _integer(baseline, "baseline_window")
            if not 2 <= baseline <= 252:
                raise ValueError("baseline_window must be between 2 and 252")
        if self.id is not None and (
            not isinstance(self.id, str)
            or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.id)
            or self.id
            in {
                "composite",
                "date",
                "instrument",
                "rank",
                "entry_date",
                "exit_date",
                "forward_return",
                "available_at",
                "name",
                "industry",
                "exclusion",
                "raw",
                "scores",
                "contributions",
            }
            or (self.id in FACTOR_LIBRARY and self.id != self.name)
        ):
            raise ValueError("factor id must be a unique lowercase identifier, not a reserved result field")
        object.__setattr__(self, "skip", skip)
        object.__setattr__(self, "baseline_window", baseline)
        if self.id == self.name:
            object.__setattr__(self, "id", None)

    @property
    def key(self):
        return self.id or self.name

    @property
    def lookback(self):
        return self.window + self.baseline_window - 1 if self.baseline_window else self.window or 0

    @property
    def parameters(self):
        return {
            key: value for key in ("window", "skip", "baseline_window") if (value := getattr(self, key)) is not None
        }

    def as_dict(self):
        result = {"name": self.name, "weight": self.weight, "window": self.window}
        if self.name in FILLABLE_FACTORS:
            result["missing_policy"] = self.missing_policy
        if self.skip is not None and (self.name != "momentum" or self.skip != 0):
            result["skip"] = self.skip
        if self.baseline_window is not None:
            result["baseline_window"] = self.baseline_window
        if self.id is not None:
            result["id"] = self.id
        return result


@dataclass(frozen=True)
class FactorConfig:
    specs: tuple[FactorSpec, ...] = (
        FactorSpec("momentum", 0.4, 20),
        FactorSpec("value", 0.3),
        FactorSpec("volatility", 0.3, 10),
    )

    def __post_init__(self) -> None:
        if isinstance(self.specs, (str, bytes)) or not isinstance(self.specs, (tuple, list)):
            raise ValueError("factor specs must be a sequence of FactorSpec")
        if not self.specs:
            raise ValueError("at least one factor must be selected")
        specs = []
        for spec in self.specs:
            if not isinstance(spec, FactorSpec):
                raise ValueError("factor specs must be FactorSpec instances")
            specs.append(spec)
        if len(specs) > 20:
            raise ValueError("at most 20 factor instances may be selected")
        names = [spec.key for spec in specs]
        if len(set(names)) != len(names):
            raise ValueError("factor names must be unique")
        if abs(sum(spec.weight for spec in specs) - 1.0) > 1e-9:
            raise ValueError("factor weights must sum to 1")
        object.__setattr__(self, "specs", tuple(sorted(specs, key=lambda spec: spec.key)))

    @property
    def weights(self) -> dict[str, float]:
        return {spec.key: spec.weight for spec in self.specs}

    def as_list(self) -> list[dict[str, Any]]:
        return [spec.as_dict() for spec in self.specs]

    @property
    def scheme(self) -> str:
        payload = json.dumps(self.as_list(), sort_keys=True)
        digest = sha256_text(payload)[:12]
        return f"market-{digest}"


@dataclass(frozen=True)
class EvaluationConfig:
    horizons: tuple[int, ...] = (1, 5, 10)
    groups: int = 3
    min_cross_section: int = 3

    def __post_init__(self) -> None:
        if isinstance(self.horizons, (str, bytes)) or not isinstance(self.horizons, (tuple, list)):
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
