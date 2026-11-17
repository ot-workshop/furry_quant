"""Strict plans and validation-only selection for retrospective split experiments."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from finance_quant.research.registry import FactorConfig, FactorSpec

PERIODS = ("development", "validation", "test")
SELECTION_METRIC = "validation_excess_return"


def _object(value, fields: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{label}须且仅须包含字段：{'、'.join(sorted(fields))}")
    return value


def _name(value, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 80:
        raise ValueError(f"{label}须为1至80个字符")
    if any(ord(char) < 32 for char in value) or "|" in value:
        raise ValueError(f"{label}包含不支持的字符")
    return value.strip()


def _date(value) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("实验日期须为YYYY-MM-DD")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError("实验日期无效") from None


@dataclass(frozen=True)
class Period:
    name: str
    start: str
    end: str


@dataclass(frozen=True)
class Candidate:
    id: str
    name: str
    factors: FactorConfig


@dataclass(frozen=True)
class ExperimentPlan:
    name: str
    periods: tuple[Period, ...]
    candidates: tuple[Candidate, ...]

    @classmethod
    def from_dict(cls, document: dict) -> ExperimentPlan:
        _object(document, {"name", "periods", "candidates", "selection_metric"}, "Experiment plan")
        name = _name(document["name"], "Plan name")
        if document["selection_metric"] != SELECTION_METRIC:
            raise ValueError(f"选择指标只能为{SELECTION_METRIC}")
        periods_obj = _object(document["periods"], set(PERIODS), "Periods")
        periods = []
        for key in PERIODS:
            obj = _object(periods_obj[key], {"start", "end"}, key)
            period = Period(key, _date(obj["start"]), _date(obj["end"]))
            if period.start > period.end or (periods and period.start <= periods[-1].end):
                raise ValueError("开发、验证、测试区间须按时间先后排列且互不重叠")
            periods.append(period)
        candidate_list = document["candidates"]
        if not isinstance(candidate_list, list) or not 2 <= len(candidate_list) <= 8:
            raise ValueError("候选方案须为2至8个")
        candidates = []
        for item in candidate_list:
            _object(item, {"id", "name", "factors"}, "Candidate")
            identifier = item["id"]
            if not isinstance(identifier, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", identifier):
                raise ValueError("候选方案编号只能使用小写字母、数字、连字符或下划线，并以字母开头")
            if not isinstance(item["factors"], list):
                raise ValueError("候选方案的因子须为列表")
            specs = []
            for spec in item["factors"]:
                if (
                    not isinstance(spec, dict)
                    or not {"name", "weight"}.issubset(spec)
                    or set(spec) - {"name", "weight", "window", "skip", "baseline_window", "id", "missing_policy"}
                ):
                    raise ValueError("因子字段仅支持名称、权重与可选窗口参数")
                specs.append(FactorSpec(**spec))
            candidates.append(Candidate(identifier, _name(item["name"], "Candidate name"), FactorConfig(tuple(specs))))
        if len({item.id for item in candidates}) != len(candidates):
            raise ValueError("候选方案编号不能重复")
        if len({item.factors.scheme for item in candidates}) != len(candidates):
            raise ValueError("候选方案的因子配置不能完全相同")
        return cls(name, tuple(periods), tuple(sorted(candidates, key=lambda item: item.id)))

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "selection_metric": SELECTION_METRIC,
            "periods": {p.name: {"start": p.start, "end": p.end} for p in self.periods},
            "candidates": [{"id": c.id, "name": c.name, "factors": c.factors.as_list()} for c in self.candidates],
        }


def load_experiment_plan(path: Path) -> ExperimentPlan:
    def unique(pairs):
        document = {}
        for key, value in pairs:
            if key in document:
                raise ValueError("实验计划包含重复字段")
            document[key] = value
        return document

    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("无法读取实验计划 JSON") from None
    return ExperimentPlan.from_dict(document)


def select_candidate(rows: list[dict], candidate_ids: list[str]) -> dict:
    """No test or development rows enter selection, including missing-data checks."""
    if len(candidate_ids) < 2 or len(set(candidate_ids)) != len(candidate_ids):
        raise ValueError("选择至少需要两个不同的候选方案")
    validation = [row for row in rows if row["period"] == "validation"]
    if len(validation) != len(candidate_ids) or sorted(row["candidate_id"] for row in validation) != sorted(
        candidate_ids
    ):
        raise ValueError("每个候选方案须恰好有一个验证区间结果")
    ranking = []
    for row in validation:
        # Derive from actual net cumulative returns; do not trust a precomputed score.
        total, benchmark = row["metrics"]["total_return"], row["metrics"]["benchmark_return"]
        if any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (total, benchmark)
        ):
            raise ValueError("验证区间收益须为有限数值")
        score = total - benchmark
        if not math.isfinite(score):
            raise ValueError("验证区间得分须为有限数值")
        ranking.append({"candidate_id": row["candidate_id"], "score": score})
    ranking.sort(key=lambda item: (-item["score"], item["candidate_id"]))
    return {
        "candidate_id": ranking[0]["candidate_id"],
        "score": ranking[0]["score"],
        "metric": SELECTION_METRIC,
        "ranking": ranking,
        "tie_break": "candidate_id_ascending",
    }


def experiment_markdown(result: dict) -> str:
    """A portable record; separate accounts and prior data exposure stay explicit."""
    names = {c["id"]: c["name"] for c in result["plan"]["candidates"]}
    periods = {"development": "开发", "validation": "验证", "test": "测试"}
    lines = [
        f"# {result['metadata']['name']}",
        "",
        f"实验：`{result['metadata']['id']}`",
        f"数据：`{result['metadata']['data_version']}`",
        "",
        "## 区间与选择规则",
        "",
    ]
    for key, period in result["plan"]["periods"].items():
        lines.append(f"- {periods[key]}：{period['start']} 至 {period['end']}")
    screened = result["metadata"].get("factor_selection")
    if screened:
        lines += [
            "",
            "## 因子组合筛选来源",
            "",
            f"来源评估：`{screened['evaluation_id']}`；摘要：`{screened['source_sha256']}`。",
            f"算法：`{screened['algorithm']}`；推荐截止日：{screened['parameters']['cutoff']}。",
            "比较区间全部晚于推荐截止日；推荐组合等权，实际组合 IC 在各段重新计算。",
            "```json",
            json.dumps(screened, ensure_ascii=False, indent=2),
            "```",
            "",
        ]
    selection = result["selection"]
    lines += [
        "",
        "规则：按验证区间累计净收益减基准累计收益从高到低选择；同分按候选 ID 升序。",
        f"锁定方案：{names[selection['candidate_id']]}（{selection['candidate_id']}）；锁定时间 {selection['locked_at']}。",
        "开发区间仅供观察；测试区间只运行锁定方案。每段重置资金与持仓，不能拼接净值。",
        "",
        "## 共用配置",
        "",
        "```json",
        json.dumps(result["shared_parameters"], ensure_ascii=False, indent=2),
        "```",
        "",
        "## 候选因子",
        "",
    ]
    for candidate in result["plan"]["candidates"]:
        lines.append(
            f"- {candidate['name']}（{candidate['id']}）：`{json.dumps(candidate['factors'], ensure_ascii=False)}`"
        )
    lines += [
        "",
        "## 各段结果",
        "",
        "| 方案 | 区间 | 交易日 | 净收益 | 基准收益 | 收益差 | 最大回撤 | 费用 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in result["rows"]:
        m = row["metrics"]
        lines.append(
            f"| {names[row['candidate_id']]} | {periods[row['period']]} | {row['trading_days']} | "
            f"{m['total_return']:.2%} | {m['benchmark_return']:.2%} | {m['excess_return']:.2%} | "
            f"{m['max_drawdown']:.2%} | {m['total_cost_amount']:.2f} |"
        )
    lines += ["", "## 可追溯结果", ""]
    for row in result["rows"]:
        lines.append(
            f"- {names[row['candidate_id']]} / {periods[row['period']]}：回测 `{row['backtest_run_id']}`；"
            f"因子 `{row['factor_run_id']}`；诊断 `{row['evaluation_run_id']}`。"
        )
    lines += ["", "## 已知限制", "", *[f"- {item}" for item in result["limitations"]], ""]
    return "\n".join(lines)
