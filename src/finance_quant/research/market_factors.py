"""Causal daily factors; deliberately excludes unverified financial reports."""

from __future__ import annotations

import numpy as np
import pandas as pd

from finance_quant.research.registry import (
    FACTOR_DIRECTIONS,
    FACTOR_DISPLAY_NAMES,
    FactorConfig,
)

# Backwards-compatible module constant; callers may still import the defaults.
MARKET_WEIGHTS = FactorConfig().weights

def _direction_text(config: FactorConfig) -> str:
    higher = [FACTOR_DISPLAY_NAMES[spec.name] for spec in config.specs
              if FACTOR_DIRECTIONS[spec.name] == "higher"]
    lower = [FACTOR_DISPLAY_NAMES[spec.name] for spec in config.specs
             if FACTOR_DIRECTIONS[spec.name] == "lower"]
    parts = []
    if higher:
        parts.append(f"{'和'.join(higher)}越高得分越高")
    if lower:
        parts.append(f"{'和'.join(lower)}越低得分越高")
    return "；".join(parts) + "。分数不是上涨概率。"


def _definitions(config: FactorConfig) -> dict[str, str]:
    result = {}
    for spec in config.specs:
        if spec.name == "momentum":
            result[spec.name] = f"历史{spec.window}个交易日复权收盘收益率"
        elif spec.name == "value":
            result[spec.name] = "当日pe_ttm的倒数；非正或缺失估值不参与"
        else:
            result[spec.name] = f"过去{spec.window}日复权收益率的年化标准差（越低越好）"
    return result


def market_factors(
    market: pd.DataFrame, data_version: str, config: FactorConfig | None = None
) -> dict:
    if config is None:
        config = FactorConfig()
    if not isinstance(config, FactorConfig):
        raise ValueError("config must be a FactorConfig")

    frame = market.sort_values(["instrument", "date"]).copy()
    if frame.empty or frame.duplicated(["instrument", "date"]).any():
        raise ValueError("Market factor data must be nonempty and unique")
    # First-observation anchoring uses no future endpoint factor.
    first_factor = frame.groupby("instrument")["adj_factor"].transform("first")
    frame["adjusted_close"] = frame["close"] * frame["adj_factor"] / first_factor

    specs = {spec.name: spec for spec in config.specs}
    if "momentum" in specs:
        frame["momentum"] = frame.groupby("instrument")["adjusted_close"].pct_change(
            specs["momentum"].window, fill_method=None
        )
    if "volatility" in specs:
        frame["daily_return"] = frame.groupby("instrument")["adjusted_close"].pct_change(
            fill_method=None
        )
        frame["volatility"] = (
            frame.groupby("instrument")["daily_return"].transform(
                lambda values: values.rolling(
                    specs["volatility"].window, min_periods=specs["volatility"].window
                ).std()
            )
            * np.sqrt(252)
        )
    if "value" in specs:
        frame["value"] = 1 / frame["pe_ttm"].where(frame["pe_ttm"] > 0)

    names = list(specs)
    history, coverage = [], {}
    for date, section in frame.groupby("date", sort=True):
        eligible = np.isfinite(section[names].to_numpy(dtype=float)).all(axis=1)
        section = section.loc[eligible]
        # Do not manufacture a cross-section from one usable stock.
        if len(section) < 2:
            continue
        scores = pd.DataFrame(index=section.index)
        for factor in names:
            ascending = FACTOR_DIRECTIONS[factor] != "lower"
            scores[factor] = section[factor].rank(pct=True, ascending=ascending) * 100
        composite = sum(
            scores[factor] * specs[factor].weight for factor in names
        )
        ranks = composite.rank(method="min", ascending=False).astype(int)
        coverage[date.strftime("%Y-%m-%d")] = {
            "eligible": len(section),
            "universe": market["instrument"].nunique(),
        }
        for index, row in section.iterrows():
            history.append({
                "instrument": row["instrument"], "name": row["name"],
                "industry": row["industry"], "date": date.strftime("%Y-%m-%d"),
                "available_at": (date.tz_localize("Asia/Shanghai") + pd.Timedelta(hours=18)).isoformat(),
                "raw": {factor: float(row[factor]) for factor in names},
                "scores": {factor: float(scores.loc[index, factor]) for factor in names},
                "composite": float(composite.loc[index]), "rank": int(ranks.loc[index]),
            })
    if not history:
        raise ValueError("Not enough history or valid valuations for real factors")
    return {
        "metadata": {
            "id": "unpublished",
            "name": "真实行情：" + "、".join(FACTOR_DISPLAY_NAMES[spec.name] for spec in config.specs),
            "as_of": history[-1]["date"],
            "universe": f"用户固定A股池（{market['instrument'].nunique()}只）",
            "sample_size": int(market["instrument"].nunique()),
            "scheme": config.scheme,
            "factor_config": config.as_list(),
            "data_version": data_version,
            "is_synthetic": False,
            "weights": config.weights,
            "coverage_by_date": coverage,
            "score_unit": "percentile_0_100",
            "direction": _direction_text(config),
            "definitions": _definitions(config),
            "availability_policy": "假设日线与每日估值在当日18:00可用，仅用于下一交易日开盘；供应商历史发布时间未审计",
            "notes": [
                "缺失估值不会填0，剔除情况见逐日覆盖率",
                "未使用财务指标接口、ROE或其他未验证财报",
            ],
        },
        "history": history,
    }
