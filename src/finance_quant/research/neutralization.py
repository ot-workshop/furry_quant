"""Causal daily industry dummy residualization with an unchanged off path."""

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class NeutralizationConfig:
    mode: str = "off"
    industry_version: str | None = None

    def __post_init__(self):
        if not isinstance(self.mode, str) or self.mode not in {"off", "industry"}:
            raise ValueError("中性化模式须为关闭或行业中性化")
        v = self.industry_version
        if v is not None and (not isinstance(v, str) or len(v) != 64 or any(c not in "0123456789abcdef" for c in v)):
            raise ValueError("历史行业版本无效")

    def as_dict(self):
        return {"mode": self.mode, **({"industry_version": self.industry_version} if self.industry_version else {})}


def parse_neutralization(value=None):
    if value is None:
        return NeutralizationConfig()
    if not isinstance(value, dict) or set(value) - {"mode", "industry_version"}:
        raise ValueError("中性化配置字段无效")
    return NeutralizationConfig(**value)


def industry_context(config=None, history=None):
    from finance_quant.data.industries import optional_industries

    config = config or NeutralizationConfig()
    history = history or optional_industries(config.industry_version)
    if config.mode == "industry" and history is None:
        raise ValueError("历史行业数据尚未下载，请先运行历史行业下载脚本")
    return history


def residualize(values, groups):
    """Winsorize direction-aligned values; industry dummies == group demeaning.

    Standard deviation uses only today's eligible cross-section, ddof=0.
    Singleton groups have zero residual and are explicitly reported.
    """
    values = values.astype(float)
    clipped = values.clip(values.quantile(0.01), values.quantile(0.99))
    residual = clipped - clipped.groupby(groups).transform("mean")
    scale = float(np.sqrt(np.mean(np.square(residual))))
    if scale <= max(1.0, float(clipped.abs().max())) * 1e-12:
        return residual * 0
    return residual / scale


def factor_industry_diagnostics(rows, metadata):
    names = list(metadata["weights"])
    groups = {}
    for row in rows:
        code = row.get("industry_code") or "unknown"
        groups.setdefault(code, []).append(row)
    output = []
    for code, members in sorted(groups.items()):
        count = len(members)

        def mean(field, key):
            return float(np.mean([row.get(field, {}).get(key, 0.0) for row in members]))

        output.append(
            {
                "industry_code": code,
                "industry_name": members[0].get("industry_name") or "未知/冲突",
                "count": count,
                "singleton": count == 1,
                "pool_weight": count / len(rows),
                "before_mean": {
                    k: mean("before_scores" if "before_scores" in members[0] else "scores", k) for k in names
                },
                "after_mean": {k: mean("scores", k) for k in names},
                "residual_mean": {k: mean("processed", k) for k in names} if "processed" in members[0] else None,
            }
        )
    return {
        "items": output,
        "total": len(output),
        "neutralization": metadata.get("neutralization", {"mode": "off"}),
        "industry_data": metadata.get("industry_data"),
        "note": "中性化去除因子值的行业均值；百分位与持仓行业权重仍可能不均衡。单股票行业残差为0。",
    }
