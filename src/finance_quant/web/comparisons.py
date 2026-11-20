"""Read-only explanation of the differences between two stored backtests.

The first version answers three questions with stored evidence only:

* what is the same, what changed and what is unknown between two results;
* are the headline numbers comparable at all;
* when one result is a derived report of the other's exact execution and only the
  evaluation benchmark changed, why the reported excess return moved.

Nothing here recalculates a backtest, rewrites a stored document or scans the
equity/trade detail series: both sides come from one narrow projection of the
result store (summary, metadata and provenance).
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

from fastapi import APIRouter, HTTPException

from finance_quant.data.symbols import from_qlib

SAME = "same"
DIFFERENT = "different"
UNKNOWN = "unknown"

# Total return is compared exactly (stored, unrounded); the excess formula keeps
# its own tolerance so the two conditions are judged separately.
FORMULA_TOLERANCE = 1e-9


def _value(document: dict, path: str) -> Any:
    node: Any = document
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _first(document: dict, *paths: str) -> Any:
    for path in paths:
        value = _value(document, path)
        if value not in (None, "", [], {}):
            return value
    return None


def _text(value: Any) -> str | None:
    """Display text for one evidence value; never turns a missing value into 0."""

    if value is None:
        return None
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return f"{value:.10g}"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _field(label: str, baseline: Any, candidate: Any, *, display: tuple[Any, Any] | None = None) -> dict[str, Any]:
    """Compare the stored values; only the shown text may be reformatted."""

    if baseline is None or candidate is None:
        status = UNKNOWN
    elif _canonical(baseline) == _canonical(candidate):
        status = SAME
    else:
        status = DIFFERENT
    shown_baseline, shown_candidate = display if display else (baseline, candidate)
    return {
        "label": label,
        "baseline": _text(shown_baseline),
        "candidate": _text(shown_candidate),
        "status": status,
    }


def _dimension(key: str, label: str, fields: list[dict]) -> dict[str, Any]:
    statuses = {field["status"] for field in fields}
    if DIFFERENT in statuses:
        status = DIFFERENT
    elif UNKNOWN in statuses:
        status = UNKNOWN
    else:
        status = SAME
    return {"key": key, "label": label, "status": status, "fields": fields}


def _factor_text(factors: Any) -> Any:
    if not isinstance(factors, list):
        return factors
    parts = []
    for factor in factors:
        if not isinstance(factor, dict):
            parts.append(str(factor))
            continue
        pieces = [str(factor.get("name", "?"))]
        weight = factor.get("weight")
        if isinstance(weight, (int, float)):
            pieces.append(f"权重 {weight * 100:.2f}%")
        window = factor.get("window")
        if window is not None:
            pieces.append(f"窗口 {window}")
        policy = factor.get("missing_policy")
        if policy:
            pieces.append(f"缺失 {policy}")
        parts.append(" / ".join(pieces))
    return "; ".join(parts) if parts else factors


def _dimensions(baseline: dict, candidate: dict) -> list[dict[str, Any]]:
    """Frozen evidence grouped by the dimensions the review asked for."""

    def pair(paths: tuple[str, ...]) -> tuple[Any, Any]:
        return _first(baseline, *paths), _first(candidate, *paths)

    market = pair(("metadata.dataset_id",))
    market_version = pair(("metadata.data_version", "metadata.provenance.data_version"))
    manifest = pair(("metadata.provenance.manifest_sha256",))
    financial_version = pair(("metadata.provenance.financial_version",))
    financial_policy = pair(("metadata.provenance.financial_availability_policy",))
    industry_version = pair(
        (
            "metadata.provenance.industry_version",
            "metadata.industry_data.id",
            "metadata.universe_filters.industry_version",
        )
    )
    industry_level = pair(("metadata.universe_filters.industry_level",))
    code_hash = pair(("metadata.provenance.code_sha256",))
    lock_hash = pair(("metadata.provenance.dependency_lock_sha256",))
    engine = pair(("metadata.engine",))
    engine_version = pair(("metadata.engine_version",))
    factors = pair(("metadata.provenance.factors",))
    neutralization = pair(("metadata.provenance.neutralization",))
    pool = pair(("metadata.provenance.pool_kind", "metadata.universe_kind"))
    universe_filters = pair(("metadata.provenance.universe_filters",))
    backtest_rules = pair(("metadata.provenance.backtest",))
    market_rules = pair(("metadata.market_rule_version",))
    costs = pair(("metadata.execution_costs",))
    benchmark_code = (
        _benchmark_code(baseline.get("metadata") or {}),
        _benchmark_code(candidate.get("metadata") or {}),
    )
    benchmark_name = pair(("metadata.benchmark_name",))
    benchmark_return_type = pair(("metadata.benchmark.return_type",))
    benchmark_series = pair(
        (
            "metadata.benchmark_provenance.series_sha256",
            "metadata.benchmark.series_sha256",
            "metadata.benchmark.snapshot.series_sha256",
        )
    )
    benchmark_version = pair(("metadata.benchmark_provenance.version", "metadata.benchmark.snapshot.schema_version"))

    factor_fields = [
        _field(
            "因子与权重",
            factors[0],
            factors[1],
            display=(_factor_text(factors[0]), _factor_text(factors[1])),
        ),
        _field("中性化", neutralization[0], neutralization[1]),
    ]
    return [
        _dimension(
            "market_data",
            "行情与冻结数据集",
            [
                _field("冻结数据集", market[0], market[1]),
                _field("行情数据版本", market_version[0], market_version[1]),
                _field("数据集清单哈希", manifest[0], manifest[1]),
            ],
        ),
        _dimension(
            "financials",
            "财务数据",
            [
                _field("财务数据版本", financial_version[0], financial_version[1]),
                _field("财务可用性口径", financial_policy[0], financial_policy[1]),
            ],
        ),
        _dimension(
            "industries",
            "行业数据",
            [
                _field("行业版本", industry_version[0], industry_version[1]),
                _field("行业层级", industry_level[0], industry_level[1]),
            ],
        ),
        _dimension(
            "code",
            "代码与运行环境",
            [
                _field("计算层代码哈希", code_hash[0], code_hash[1]),
                _field("依赖锁哈希", lock_hash[0], lock_hash[1]),
                _field("引擎", engine[0], engine[1]),
                _field("引擎版本", engine_version[0], engine_version[1]),
            ],
        ),
        _dimension("factors", "因子与参数", factor_fields),
        _dimension(
            "trading_rules",
            "交易规则",
            [
                _field("股票池类型", pool[0], pool[1]),
                _field("股票池筛选", universe_filters[0], universe_filters[1]),
                _field("回测参数", backtest_rules[0], backtest_rules[1]),
                _field("市场规则版本", market_rules[0], market_rules[1]),
                _field("成本模型", costs[0], costs[1]),
            ],
        ),
        _dimension(
            "benchmark",
            "评价基准",
            [
                _field("基准代码", benchmark_code[0], benchmark_code[1]),
                _field("基准名称", benchmark_name[0], benchmark_name[1]),
                _field("收益类型", benchmark_return_type[0], benchmark_return_type[1]),
                _field("基准序列哈希", benchmark_series[0], benchmark_series[1]),
                _field("序列版本", benchmark_version[0], benchmark_version[1]),
            ],
        ),
    ]


def _summary(document: dict) -> dict[str, Any]:
    summary = dict(document.get("summary") or {})
    metrics = dict(summary.get("metrics") or {})
    return {**summary, "metrics": metrics}


def _pointers(metadata: dict) -> list[str]:
    """Every distinct non-empty execution pointer stored on one side."""

    values = [
        _value(metadata, "execution_run_id"),
        _value(metadata, "benchmark_provenance.execution_run_id"),
        _value(metadata, "provenance.execution_run_id"),
    ]
    return sorted({value for value in values if isinstance(value, str) and value})


def _benchmark_code(metadata: dict) -> Any:
    """Benchmark code from modern or legacy paths, in one canonical form."""

    value = _first(
        metadata,
        "benchmark_provenance.code",
        "benchmark.code",
        "provenance.benchmark",
        "config.benchmark",
    )
    if isinstance(value, dict):
        value = value.get("code")
    if not isinstance(value, str) or not value.strip():
        return None
    code = value.strip()
    # Legacy results kept Qlib's SH000300 form; compare it as 000300.SH.
    if re.fullmatch(r"[A-Za-z]{2}\d{6}", code):
        try:
            return from_qlib(code.upper())
        except ValueError:
            return code
    return code


def _validate_document(document: dict) -> None:
    """Reject a stored document whose nested fields are the wrong shape.

    Runs on the original archive, before any missing evidence is filled in, so a
    broken field cannot be masked by a successful related-archive lookup.
    """

    metadata = document.get("metadata")
    if not isinstance(metadata, dict):
        raise ValueError("metadata is not an object")
    provenance = metadata.get("provenance")
    if provenance is not None and not isinstance(provenance, dict):
        raise ValueError("metadata.provenance is not an object")
    summary = document.get("summary")
    if not isinstance(summary, dict):
        raise ValueError("summary is not an object")
    metrics = summary.get("metrics") or {}
    if not isinstance(metrics, dict):
        raise ValueError("summary.metrics is not an object")
    for key in ("total_return", "benchmark_return", "excess_return", "max_drawdown"):
        value = metrics.get(key)
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"metric {key} is not finite")


def _side(document: dict, identifier: str) -> dict[str, Any]:
    summary = _summary(document)
    metadata = document.get("metadata") or {}
    benchmark = metadata.get("benchmark") or {}
    return {
        "id": identifier,
        "name": summary.get("name"),
        "status": summary.get("status"),
        "start_date": summary.get("start_date"),
        "end_date": summary.get("end_date"),
        "created_at": summary.get("created_at"),
        "benchmark": {
            "code": _benchmark_code(metadata),
            "name": metadata.get("benchmark_name"),
            "return_type": benchmark.get("return_type"),
            "series_sha256": _first(
                metadata,
                "benchmark_provenance.series_sha256",
                "benchmark.series_sha256",
                "benchmark.snapshot.series_sha256",
            ),
        },
        "metrics": {
            "total_return": summary["metrics"].get("total_return"),
            "benchmark_return": summary["metrics"].get("benchmark_return"),
            "excess_return": summary["metrics"].get("excess_return"),
            "max_drawdown": summary["metrics"].get("max_drawdown"),
        },
        "execution_run_id": (_pointers(metadata) or [None])[0],  # type: ignore[list-item]
        "execution_pointers": _pointers(metadata),
        "performance_version": _value(document, "performance.version") or _value(metadata, "performance.version"),
    }


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _metric(key: str, label: str, unit: str, baseline: Any, candidate: Any, comparable: bool) -> dict[str, Any]:
    left, right = _number(baseline), _number(candidate)
    shown = comparable and left is not None and right is not None
    delta = None
    if shown and left is not None and right is not None:
        delta = (right - left) * 100 if unit == "percent" else right - left
    return {
        "key": key,
        "label": label,
        "unit": unit,
        "baseline": left,
        "candidate": right,
        # Direction is always candidate − baseline ("B − A"); an incomparable pair
        # keeps both stored values but never gets a delta.
        "delta": delta,
        "comparable": shown,
    }


def _comparability(side_a: dict, side_b: dict) -> dict[str, Any]:
    """Interval and metric version must both be stored and equal before any delta is shown."""

    reasons = []
    interval_a, interval_b = (
        (side_a["start_date"], side_a["end_date"]),
        (
            side_b["start_date"],
            side_b["end_date"],
        ),
    )
    if not all(interval_a) or not all(interval_b):
        interval = UNKNOWN
        reasons.append("至少一侧缺少回测区间，收益类差值不可比")
    elif interval_a != interval_b:
        interval = DIFFERENT
        reasons.append("两次回测的区间不同，收益类差值不可直接比较")
    else:
        interval = SAME

    version_a, version_b = side_a["performance_version"], side_b["performance_version"]
    if not version_a or not version_b:
        metric_version = UNKNOWN
        reasons.append("至少一侧缺少绩效口径版本，差值标为不可比")
    elif version_a != version_b:
        metric_version = DIFFERENT
        reasons.append(f"绩效口径版本不同（{version_a} / {version_b}），差值不可比")
    else:
        metric_version = SAME

    return {
        "comparable": interval == SAME and metric_version == SAME,
        "interval": interval,
        "metric_version": metric_version,
        "reasons": reasons,
        "baseline_interval": list(interval_a),
        "candidate_interval": list(interval_b),
    }


def _relation(
    side_a: dict,
    side_b: dict,
    comparability: dict,
    dimensions: list[dict],
) -> dict[str, Any]:
    """Same-execution detection; a cause is claimed only when every gate passes."""

    left, right = side_a["execution_run_id"], side_b["execution_run_id"]
    if len(side_a.get("execution_pointers") or []) > 1 or len(side_b.get("execution_pointers") or []) > 1:
        # Conflicting pointers inside one stored result cannot identify its source.
        return {
            "status": "conflict",
            "execution_pointer": left or right,
            "evidence": [
                {
                    "label": "单份结果内执行来源指针互相矛盾",
                    "baseline": side_a.get("execution_pointers"),
                    "candidate": side_b.get("execution_pointers"),
                }
            ],
            "explanation": None,
        }
    points_right = bool(left) and left == side_b["id"]
    points_left = bool(right) and right == side_a["id"]
    if (points_right and right) or (points_left and left):
        # Two pointers that contradict each other (a cycle or a one-sided back
        # reference) cannot prove that either result derives from the other.
        return {
            "status": "conflict",
            "execution_pointer": left or right,
            "evidence": [
                {
                    "label": "执行来源指针相互矛盾",
                    "baseline": left,
                    "candidate": right,
                }
            ],
            "explanation": None,
        }
    derived: dict[str, Any] | None
    source: dict[str, Any] | None
    pointer: Any = None
    if points_right:
        derived, source, pointer = side_a, side_b, left
    elif points_left:
        derived, source, pointer = side_b, side_a, right
    else:
        derived, source = None, None

    if derived is None or source is None:
        shared = left and right and left == right
        status = "sibling" if shared else ("independent" if (left or right) else UNKNOWN)
        evidence = [
            {
                "label": "执行来源指针",
                "baseline": left,
                "candidate": right,
            }
        ]
        if shared:
            evidence.append(
                {
                    "label": "两侧派生自同一个执行结果",
                    "value": left,
                }
            )
        return {
            "status": status,
            "execution_pointer": left or right,
            "evidence": evidence,
            "explanation": None,
        }

    evidence = [
        {"label": "派生报告记录的执行来源", "value": pointer},
        {"label": "派生报告", "value": derived["id"]},
        {"label": "原执行结果", "value": source["id"]},
    ]
    total_a, total_b = _number(derived["metrics"]["total_return"]), _number(source["metrics"]["total_return"])
    same_total = total_a is not None and total_b is not None and total_a == total_b
    evidence.append(
        {
            "label": "策略总收益是否完全一致",
            "value": "一致" if same_total else "不一致或缺失",
            "precision": [total_a, total_b],
        }
    )

    formula_ok = True
    for side in (derived, source):
        total = _number(side["metrics"]["total_return"])
        benchmark = _number(side["metrics"]["benchmark_return"])
        excess = _number(side["metrics"]["excess_return"])
        holds = (
            total is not None
            and benchmark is not None
            and excess is not None
            and abs(excess - (total - benchmark)) <= FORMULA_TOLERANCE
        )
        formula_ok = formula_ok and holds
        evidence.append(
            {
                "label": f"{side['id']}：已存超额 = 策略总收益 − 基准收益",
                "value": "成立" if holds else "不成立或字段缺失",
            }
        )

    conflict = not same_total or not formula_ok
    evidence.append(
        {
            "label": "两侧区间与绩效口径是否可比",
            "value": "可比" if comparability["comparable"] else "不可比",
        }
    )
    changed = [
        dimension["label"]
        for dimension in dimensions
        if dimension["key"] != "benchmark" and dimension["status"] == DIFFERENT
    ]
    if changed:
        evidence.append(
            {
                "label": "评价基准之外仍变化的维度",
                "value": "、".join(changed),
            }
        )

    if conflict or not comparability["comparable"] or changed:
        return {
            "status": "conflict",
            "execution_pointer": pointer,
            "evidence": evidence,
            "derived_run_id": derived["id"],
            "source_run_id": source["id"],
            "explanation": None,
        }

    benchmark_a = _number(side_a["metrics"]["benchmark_return"])
    benchmark_b = _number(side_b["metrics"]["benchmark_return"])
    excess_a = _number(side_a["metrics"]["excess_return"])
    excess_b = _number(side_b["metrics"]["excess_return"])
    explanation = None
    benchmark_pp = excess_pp = None
    if benchmark_a is not None and benchmark_b is not None and excess_a is not None and excess_b is not None:
        # The relation reports the same B − A direction as every other number on
        # the page, whichever side happens to be the derived report.
        benchmark_pp = (benchmark_b - benchmark_a) * 100
        excess_pp = (excess_b - excess_a) * 100
        if abs(benchmark_pp + excess_pp) <= 1e-6:
            explanation = (
                f"基准收益 B − A：{benchmark_pp:+.4f} 个百分点；"
                f"报表超额 B − A：{excess_pp:+.4f} 个百分点。"
                f"两者互为相反数，是报表口径的算术差，不是策略交易变化的因果证明。"
            )
            evidence.append(
                {
                    "label": "基准收益差与超额收益差互为相反数",
                    "value": f"基准 {benchmark_pp:+.10f} pp / 超额 {excess_pp:+.10f} pp",
                }
            )
    return {
        "status": "same_execution",
        "execution_pointer": pointer,
        "evidence": evidence,
        "derived_run_id": derived["id"],
        "source_run_id": source["id"],
        "benchmark_delta_pp": benchmark_pp,
        "excess_delta_pp": excess_pp,
        "explanation": explanation,
    }


def compare_documents(baseline_id: str, candidate_id: str, baseline: dict, candidate: dict) -> dict[str, Any]:
    side_a = _side(baseline, baseline_id)
    side_b = _side(candidate, candidate_id)
    comparability = _comparability(side_a, side_b)
    comparable = comparability["comparable"]
    metrics = [
        _metric(
            "total_return",
            "策略总收益",
            "percent",
            side_a["metrics"]["total_return"],
            side_b["metrics"]["total_return"],
            comparable,
        ),
        _metric(
            "benchmark_return",
            "基准收益",
            "percent",
            side_a["metrics"]["benchmark_return"],
            side_b["metrics"]["benchmark_return"],
            comparable,
        ),
        _metric(
            "excess_return",
            "收益差（超额收益）",
            "percent",
            side_a["metrics"]["excess_return"],
            side_b["metrics"]["excess_return"],
            comparable,
        ),
    ]
    dimensions = _dimensions(baseline, candidate)
    relation = _relation(side_a, side_b, comparability, dimensions)
    changed = [item["label"] for item in dimensions if item["status"] == DIFFERENT]
    unknown = [item["label"] for item in dimensions if item["status"] == UNKNOWN]
    if relation["status"] == "same_execution":
        conclusion = "两次结果来自同一执行结果，报表差可由已存基准收益差解释"
    elif relation["status"] == "sibling":
        conclusion = "两次结果都派生自同一个执行结果，差异需按各自基准逐项核对"
    elif changed and unknown:
        conclusion = f"发现多项差异（{len(changed)} 项不同、{len(unknown)} 项未知），不据此判断单一原因"
    elif changed:
        conclusion = f"发现 {len(changed)} 项差异，不据此判断单一原因"
    elif unknown:
        conclusion = "现有证据不足，无法判断差异原因"
    else:
        conclusion = "两次结果的已存证据完全一致"
    return {
        "baseline": side_a,
        "candidate": side_b,
        "direction": "candidate_minus_baseline",
        "conclusion": conclusion,
        "conclusion_states_cause": relation["status"] == "same_execution",
        "comparability": comparability,
        "metrics": metrics,
        "dimensions": dimensions,
        "relation": relation,
        "not_implemented": [
            "最早评分差异或最早成交差异的定位：首版不扫描全量净值与成交明细",
            "旧价格基准的序列版本：旧结果未保存来源版本时保持未知",
        ],
    }


RECIPE_PATHS = (
    "metadata.factors",
    "metadata.factor_config.factors",
    "metadata.factor_config",
    "metadata.request.factors",
    "metadata.specs",
)


def _recipe(document: dict) -> Any:
    for path in RECIPE_PATHS:
        value = _value(document, path)
        if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
            return value
    return None


DETAIL_KEYS = ("equity", "trades", "positions", "orders", "tables")


def project_document(store, category: str, run_id: str) -> dict[str, Any]:
    """One read of a stored document without its detail series.

    PostgreSQL stores implement ``project`` (explicit columns, excluded from the
    computation fingerprint). The file store only exposes its path, so the
    projection happens here instead of changing ``storage.py``.
    """

    project = getattr(store, "project", None)
    if callable(project):
        return project(category, run_id)
    document = json.loads(store.path(category, run_id).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("result document is not an object")
    for key in DETAIL_KEYS:
        document.pop(key, None)
    return document


def attach_factor_recipe(document: dict, store) -> dict:
    """Fill a missing factor recipe from the frozen factor run it points at.

    Fixed-pool backtests do not always repeat the recipe in their own provenance;
    the frozen ``factor_run_id`` is the authoritative record, and its stored
    neutralization travels with the recipe. Missing or broken factor runs leave
    the fields unknown rather than guessing.
    """

    metadata = document.get("metadata") or {}
    provenance = metadata.get("provenance") if isinstance(metadata, dict) else {}
    provenance = provenance if isinstance(provenance, dict) else {}
    factor_run_id = provenance.get("factor_run_id") or (
        metadata.get("factor_run_id") if isinstance(metadata, dict) else None
    )
    if not factor_run_id:
        return document
    if provenance.get("factors") and provenance.get("neutralization"):
        return document
    try:
        factor_run = project_document(store, "factors", factor_run_id)
    except (FileNotFoundError, OSError, ValueError, KeyError, TypeError, AttributeError):
        return document
    # A related archive that is not the expected mapping leaves the recipe
    # unknown; it must never turn the main comparison into an error.
    if not isinstance(factor_run, dict):
        return document
    factor_metadata = factor_run.get("metadata")
    factor_metadata = factor_metadata if isinstance(factor_metadata, dict) else {}
    # Only fill what the result does not already record: a stored recipe or
    # neutralization is never overwritten by the factor archive.
    filled = {}
    if not provenance.get("factors"):
        recipe = _recipe(factor_run)
        if recipe:
            filled["factors"] = recipe
    if not provenance.get("neutralization"):
        neutralization = factor_metadata.get("neutralization")
        if neutralization:
            filled["neutralization"] = neutralization
    if not filled:
        return document
    return {
        **document,
        "metadata": {
            **metadata,
            "provenance": {
                **provenance,
                **filled,
                "factor_recipe_source": factor_run_id,
            },
        },
    }


def comparison_router(store) -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    def load(run_id: str) -> dict:
        """One narrow projection per side; detail series stay in storage."""

        document: dict[str, Any] | None
        try:
            document = project_document(store, "backtests", run_id)
        except FileNotFoundError:
            raise HTTPException(404, f"回测结果不存在：{run_id}") from None
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            document = None
        if (
            not isinstance(document, dict)
            or not isinstance(document.get("metadata"), dict)
            or not isinstance(document.get("summary"), dict)
        ):
            try:
                exists = store.exists("backtests", run_id)
            except (FileNotFoundError, OSError, ValueError, KeyError, TypeError):
                exists = False
            detail = "回测结果文件损坏或格式异常，请重新运行该配置" if exists else "回测结果不存在"
            raise HTTPException(409 if exists else 404, detail) from None
        try:
            _validate_document(document)
        except (ValueError, KeyError, TypeError, AttributeError):
            raise HTTPException(409, "回测结果格式异常（嵌套字段类型不符），无法比较") from None
        return document

    @router.get("/backtest-comparisons")
    def compare(baseline_run_id: str, candidate_run_id: str):
        if baseline_run_id == candidate_run_id:
            raise HTTPException(422, "请选择两条不同的回测")
        baseline = attach_factor_recipe(load(baseline_run_id), store)
        candidate = attach_factor_recipe(load(candidate_run_id), store)
        try:
            return compare_documents(baseline_run_id, candidate_run_id, baseline, candidate)
        except (AttributeError, KeyError, TypeError, ValueError):
            # A stored document whose nested fields are not the expected shapes is
            # a corrupt archive, not a server fault.
            raise HTTPException(409, "回测结果格式异常（嵌套字段类型不符），无法比较") from None

    return router
