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
        raise ValueError(f"{label} must contain exactly: {', '.join(sorted(fields))}")
    return value


def _name(value, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 80:
        raise ValueError(f"{label} must be a nonempty string of at most 80 characters")
    if any(ord(char) < 32 for char in value) or "|" in value:
        raise ValueError(f"{label} contains unsupported characters")
    return value.strip()


def _date(value) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Experiment dates must use YYYY-MM-DD")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError("Invalid experiment date") from None


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
            raise ValueError(f"selection_metric must be {SELECTION_METRIC}")
        periods_obj = _object(document["periods"], set(PERIODS), "Periods")
        periods = []
        for key in PERIODS:
            obj = _object(periods_obj[key], {"start", "end"}, key)
            period = Period(key, _date(obj["start"]), _date(obj["end"]))
            if period.start > period.end or (periods and period.start <= periods[-1].end):
                raise ValueError("Periods must be chronological, disjoint and nonempty")
            periods.append(period)
        candidate_list = document["candidates"]
        if not isinstance(candidate_list, list) or not 2 <= len(candidate_list) <= 8:
            raise ValueError("Use between 2 and 8 explicitly declared candidates")
        candidates = []
        for item in candidate_list:
            _object(item, {"id", "name", "factors"}, "Candidate")
            identifier = item["id"]
            if not isinstance(identifier, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", identifier):
                raise ValueError("Candidate id must use lowercase letters, digits, hyphens or underscores")
            if not isinstance(item["factors"], list):
                raise ValueError("Candidate factors must be an array")
            specs = []
            for spec in item["factors"]:
                if not isinstance(spec, dict) or set(spec) not in ({"name", "weight"}, {"name", "weight", "window"}):
                    raise ValueError("Factor fields must be name, weight and optional window")
                specs.append(FactorSpec(**spec))
            candidates.append(Candidate(identifier, _name(item["name"], "Candidate name"), FactorConfig(tuple(specs))))
        if len({item.id for item in candidates}) != len(candidates):
            raise ValueError("Candidate ids must be unique")
        if len({item.factors.scheme for item in candidates}) != len(candidates):
            raise ValueError("Candidate factor configurations must be distinct")
        return cls(name, tuple(periods), tuple(sorted(candidates, key=lambda item: item.id)))

    def as_dict(self) -> dict:
        return {"name": self.name, "selection_metric": SELECTION_METRIC,
                "periods": {p.name: {"start": p.start, "end": p.end} for p in self.periods},
                "candidates": [{"id": c.id, "name": c.name, "factors": c.factors.as_list()} for c in self.candidates]}


def load_experiment_plan(path: Path) -> ExperimentPlan:
    def unique(pairs):
        document = {}
        for key, value in pairs:
            if key in document:
                raise ValueError("Experiment plan contains duplicate fields")
            document[key] = value
        return document
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("Unable to read experiment plan JSON") from None
    return ExperimentPlan.from_dict(document)


def select_candidate(rows: list[dict], candidate_ids: list[str]) -> dict:
    """No test or development rows enter selection, including missing-data checks."""
    if len(candidate_ids) < 2 or len(set(candidate_ids)) != len(candidate_ids):
        raise ValueError("Selection requires at least two unique candidate ids")
    validation = [row for row in rows if row["period"] == "validation"]
    if len(validation) != len(candidate_ids) or sorted(row["candidate_id"] for row in validation) != sorted(candidate_ids):
        raise ValueError("Every candidate needs exactly one validation result")
    ranking = []
    for row in validation:
        # Derive from actual net cumulative returns; do not trust a precomputed score.
        total, benchmark = row["metrics"]["total_return"], row["metrics"]["benchmark_return"]
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (total, benchmark)):
            raise ValueError("Validation returns must be finite numbers")
        score = total - benchmark
        if not math.isfinite(score):
            raise ValueError("Validation score must be finite")
        ranking.append({"candidate_id": row["candidate_id"], "score": score})
    ranking.sort(key=lambda item: (-item["score"], item["candidate_id"]))
    return {"candidate_id": ranking[0]["candidate_id"], "score": ranking[0]["score"],
            "metric": SELECTION_METRIC, "ranking": ranking, "tie_break": "candidate_id_ascending"}


def experiment_markdown(result: dict) -> str:
    """A portable record; separate accounts and prior data exposure stay explicit."""
    names = {c["id"]: c["name"] for c in result["plan"]["candidates"]}
    periods = {"development": "开发", "validation": "验证", "test": "测试"}
    lines = [f"# {result['metadata']['name']}", "", f"实验：`{result['metadata']['id']}`",
             f"数据：`{result['metadata']['data_version']}`", "", "## 区间与选择规则", ""]
    for key, period in result["plan"]["periods"].items():
        lines.append(f"- {periods[key]}：{period['start']} 至 {period['end']}")
    selection = result["selection"]
    lines += ["", "规则：按验证区间累计净收益减基准累计收益从高到低选择；同分按候选 ID 升序。",
              f"锁定方案：{names[selection['candidate_id']]}（{selection['candidate_id']}）；锁定时间 {selection['locked_at']}。",
              "开发区间仅供观察；测试区间只运行锁定方案。每段重置资金与持仓，不能拼接净值。", "",
              "## 共用配置", "", "```json", json.dumps(result["shared_parameters"], ensure_ascii=False, indent=2), "```", "",
              "## 候选因子", ""]
    for candidate in result["plan"]["candidates"]:
        lines.append(f"- {candidate['name']}（{candidate['id']}）：`{json.dumps(candidate['factors'], ensure_ascii=False)}`")
    lines += ["", "## 各段结果", "", "| 方案 | 区间 | 交易日 | 净收益 | 基准收益 | 收益差 | 最大回撤 | 费用 |",
              "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for row in result["rows"]:
        m = row["metrics"]
        lines.append(f"| {names[row['candidate_id']]} | {periods[row['period']]} | {row['trading_days']} | "
                     f"{m['total_return']:.2%} | {m['benchmark_return']:.2%} | {m['excess_return']:.2%} | "
                     f"{m['max_drawdown']:.2%} | {m['total_cost_amount']:.2f} |")
    lines += ["", "## 可追溯结果", ""]
    for row in result["rows"]:
        lines.append(f"- {names[row['candidate_id']]} / {periods[row['period']]}：回测 `{row['backtest_run_id']}`；"
                     f"因子 `{row['factor_run_id']}`；诊断 `{row['evaluation_run_id']}`。")
    lines += ["", "## 已知限制", "", *[f"- {item}" for item in result["limitations"]], ""]
    return "\n".join(lines)
