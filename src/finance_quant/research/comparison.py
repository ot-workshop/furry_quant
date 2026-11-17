"""Explicit retrospective controls; return differences are not causal alpha attribution."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ComparisonSettings:
    baseline_id: str
    sample_mode: str = "common"

    def __post_init__(self):
        if not isinstance(self.baseline_id, str) or not self.baseline_id:
            raise ValueError("请选择基准方案")
        if not isinstance(self.sample_mode, str) or self.sample_mode not in {"common", "independent"}:
            raise ValueError("样本方式须为共同样本或各方案独立样本")

    def as_dict(self):
        return {"baseline_id": self.baseline_id, "sample_mode": self.sample_mode}


def return_differences(rows, settings):
    lookup = {(row["candidate_id"], row["sample"]): row for row in rows}
    baseline = lookup[settings.baseline_id, "independent"]["metrics"]
    common_base = lookup.get((settings.baseline_id, "common"))
    output = []
    for candidate, sample in lookup:
        if sample != "independent":
            continue
        independent = lookup[candidate, sample]["metrics"]["total_return"] - baseline["total_return"]
        common = lookup.get((candidate, "common"))
        controlled = (
            common["metrics"]["total_return"] - common_base["metrics"]["total_return"]
            if common and common_base
            else None
        )
        output.append(
            {
                "candidate_id": candidate,
                "independent_difference": independent,
                "common_difference": controlled,
                "sample_difference": independent - controlled if controlled is not None else None,
            }
        )
    return output


def comparison_markdown(result):
    names = {c["id"]: c["name"] for c in result["plan"]["candidates"]}
    lines = [
        "# 收益来源对照",
        "",
        result["metadata"]["name"],
        "",
        "各方案使用同一研究区间和交易参数；这是历史对照，不进行样本外选优。",
        "",
        "| 方案 | 独立样本收益差 | 共同样本收益差 | 样本变化差额 |",
        "| --- | ---: | ---: | ---: |",
    ]
    for item in result["comparisons"]:
        cells = [
            "—" if item[key] is None else f"{item[key]:.2%}"
            for key in ("independent_difference", "common_difference", "sample_difference")
        ]
        lines.append(f"| {names[item['candidate_id']]} | " + " | ".join(cells) + " |")
    lines += ["", "## 运行结果", ""]
    for row in result["rows"]:
        lines.append(
            f"- {names[row['candidate_id']]} / {row['sample']}：回测 `{row['backtest_run_id']}`；因子 `{row['factor_run_id']}`；评估 `{row['evaluation_run_id']}`。"
        )
    lines += ["", "## 对照口径", "", *[f"- {note}" for note in result["limitations"]], ""]
    return "\n".join(lines)
