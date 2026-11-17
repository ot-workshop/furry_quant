"""Causal daily industry dummy residualization with an unchanged off path."""

from dataclasses import dataclass

import numpy as np

from finance_quant.data.industries import optional_industries

INDUSTRY_LEVELS = ("L1", "L2", "L3")
INDUSTRY_LEVEL_LABELS = {"L1": "一级", "L2": "二级", "L3": "三级"}


@dataclass(frozen=True)
class NeutralizationConfig:
    mode: str = "off"
    industry_version: str | None = None
    industry_level: str = "L1"

    def __post_init__(self):
        if not isinstance(self.mode, str) or self.mode not in {"off", "industry"}:
            raise ValueError("中性化模式须为关闭或行业中性化")
        v = self.industry_version
        if v is not None and (not isinstance(v, str) or len(v) != 64 or any(c not in "0123456789abcdef" for c in v)):
            raise ValueError("历史行业版本无效")
        if self.industry_level not in INDUSTRY_LEVELS:
            raise ValueError("行业级别须为 L1、L2 或 L3")
        if self.mode == "off" and self.industry_level != "L1":
            raise ValueError("关闭中性化时不能指定行业级别")

    def as_dict(self):
        # L1 is the historical default: omitting it keeps old fingerprints reusable.
        return {
            "mode": self.mode,
            **({"industry_version": self.industry_version} if self.industry_version else {}),
            **(
                {"industry_level": self.industry_level}
                if self.mode == "industry" and self.industry_level != "L1"
                else {}
            ),
        }


def parse_neutralization(value=None):
    if value is None:
        return NeutralizationConfig()
    if not isinstance(value, dict) or set(value) - {"mode", "industry_version", "industry_level"}:
        raise ValueError("中性化配置字段无效")
    return NeutralizationConfig(**value)


def industry_context(config=None, history=None):

    config = config or NeutralizationConfig()
    history = history or optional_industries(config.industry_version)
    if config.mode == "industry" and history is None:
        raise ValueError("历史行业数据尚未下载，请先在数据管理中下载行业数据")
    if config.mode == "industry":
        history.validate_level(config.industry_level)
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


def _mean_factor(members, field, key):
    values = [row.get(field, {}).get(key) for row in members]
    observed = [value for value in values if value is not None and np.isfinite(value)]
    return float(np.mean(observed)) if observed else None


def factor_industry_diagnostics(rows, metadata):
    names = list(metadata["weights"])
    groups = {}
    for row in rows:
        code = row.get("industry_code") or "unknown"
        groups.setdefault(code, []).append(row)
    output = []
    for code, members in sorted(groups.items()):
        count = len(members)

        output.append(
            {
                "industry_code": code,
                "industry_name": members[0].get("industry_name") or "未知/冲突",
                "count": count,
                "singleton": count == 1,
                "pool_weight": count / len(rows),
                "before_mean": {
                    k: _mean_factor(members, "before_scores" if "before_scores" in members[0] else "scores", k)
                    for k in names
                },
                "after_mean": {k: _mean_factor(members, "scores", k) for k in names},
                "residual_mean": {k: _mean_factor(members, "processed", k) for k in names}
                if "processed" in members[0]
                else None,
            }
        )
    return {
        "items": output,
        "total": len(output),
        "neutralization": metadata.get("neutralization", {"mode": "off"}),
        "industry_data": metadata.get("industry_data"),
        "note": "中性化去除因子值的行业均值；百分位与持仓行业权重仍可能不均衡。单股票行业残差为0。",
    }
