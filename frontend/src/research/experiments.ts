import type { SelectionReference } from "../api/factorSelection";
import type {
  ExperimentRequest,
  ResearchOptions,
  Neutralization,
} from "../api/research";
import {
  buildRequest,
  earliestStart,
  tradingDates,
  type FactorRow,
  type FormState,
} from "./form";

export interface ExperimentCandidateDraft {
  id: string;
  name: string;
  factors: FactorRow[];
  neutralization?: Neutralization;
}

export function buildReturnComparison(
  form: FormState,
  options: ResearchOptions,
  candidates: ExperimentCandidateDraft[],
  baseline: string,
  common: boolean,
) {
  const errors: string[] = [];
  if (candidates.length < 2 || candidates.length > 8)
    errors.push("对照方案需为2–8个。");
  if (!candidates.some((c) => c.id === baseline))
    errors.push("请选择基准方案。");
  const built = candidates.map((candidate) => {
    const neutral = candidate.neutralization;
    const state = {
      ...form,
      factors: candidate.factors,
      ...(neutral
        ? {
            neutralization: neutral.mode,
            industryLevel: neutral.industry_level ?? ("L1" as const),
          }
        : {}),
    };
    const result = buildRequest(state, options);
    errors.push(...result.errors.map((e) => `${candidate.name}：${e}`));
    if (!candidate.name.trim()) errors.push("请填写每个方案的名称。");
    return result.request;
  });
  const fingerprints = built.filter(Boolean).map((r) =>
    JSON.stringify({
      factors: r!.factors,
      neutralization: r!.neutralization ?? { mode: "off" },
    }),
  );
  if (new Set(fingerprints).size !== fingerprints.length)
    errors.push("对照方案的因子、权重和行业处理不能完全相同。");
  const first = built[0];
  const earliest =
    candidates
      .map((c) => earliestStart({ ...form, factors: c.factors }, options))
      .filter((v): v is string => Boolean(v))
      .sort()
      .at(-1) ?? null;
  const request: ExperimentRequest | null =
    errors.length || !first
      ? null
      : {
          name: form.name,
          data: first.data,
          evaluation: first.evaluation,
          backtest: Object.fromEntries(
            Object.entries(first.backtest).filter(
              ([key]) => key !== "start" && key !== "end",
            ),
          ) as ExperimentRequest["backtest"],
          ...(form.industryVersion || options.industry_data?.id
            ? {
                neutralization: {
                  mode: "off",
                  industry_version:
                    form.industryVersion || options.industry_data?.id,
                },
              }
            : {}),
          plan: {
            periods: { analysis: { start: form.start, end: form.end } },
            comparison: {
              baseline_id: baseline,
              sample_mode: common ? "common" : "independent",
            },
            candidates: candidates.map((c, i) => ({
              id: c.id,
              name: c.name.trim(),
              factors: built[i]!.factors,
              neutralization: built[i]!.neutralization ?? { mode: "off" },
            })),
          },
        };
  return { request, errors: [...new Set(errors)], earliest };
}

export function buildExperiment(
  form: FormState,
  options: ResearchOptions,
  candidates: ExperimentCandidateDraft[],
  splits: { validation: string; test: string },
  selection?: SelectionReference,
) {
  const errors: string[] = [];
  const dates = tradingDates(form, options);
  const previous = (day: string) =>
    dates.filter((value) => value < day).at(-1) ?? "";
  const periods =
    form.start && form.end && splits.validation && splits.test
      ? {
          development: { start: form.start, end: previous(splits.validation) },
          validation: { start: splits.validation, end: previous(splits.test) },
          test: { start: splits.test, end: form.end },
        }
      : null;
  if (!periods) errors.push("请设置总区间以及验证段、测试段的开始日期。");
  if (candidates.length < 2 || candidates.length > 8)
    errors.push("候选方案需为 2–8 个。");
  const built = candidates.map((candidate) => {
    const result = buildRequest(
      { ...form, factors: candidate.factors },
      options,
    );
    errors.push(
      ...result.errors.map(
        (error) => `${candidate.name || candidate.id}：${error}`,
      ),
    );
    if (!candidate.name.trim()) errors.push("请填写每个方案的名称。");
    return result.request;
  });
  const base = built[0];
  const earliest =
    candidates
      .map((candidate) =>
        earliestStart({ ...form, factors: candidate.factors }, options),
      )
      .filter((value): value is string => Boolean(value))
      .sort()
      .at(-1) ?? null;
  if (periods) {
    for (const [name, period] of Object.entries(periods)) {
      if (
        !dates.includes(period.start) ||
        !dates.includes(period.end) ||
        period.start > period.end
      )
        errors.push(
          `${name === "development" ? "开发" : name === "validation" ? "验证" : "测试"}段须选择数据范围内按先后排列的交易日。`,
        );
      const count = dates.filter(
        (day) => day >= period.start && day <= period.end,
      ).length;
      if (base && count <= Math.max(...base.evaluation.horizons))
        errors.push("每个分段的交易日数须超过最长评估持有期。");
    }
  }
  const fingerprints = built
    .filter(Boolean)
    .map((request) => JSON.stringify(request!.factors));
  if (new Set(fingerprints).size !== fingerprints.length)
    errors.push("候选方案的因子配置不能完全相同。");
  if (selection && form.start <= selection.parameters.cutoff)
    errors.push("比较区间须晚于推荐截止日。");
  const request: ExperimentRequest | null =
    errors.length || !base || !periods
      ? null
      : {
          name: base.name,
          ...(selection ? { factor_selection: selection } : {}),
          data: base.data,
          backtest: Object.fromEntries(
            Object.entries(base.backtest).filter(
              ([key]) => key !== "start" && key !== "end",
            ),
          ) as ExperimentRequest["backtest"],
          evaluation: base.evaluation,
          ...(base.neutralization
            ? { neutralization: base.neutralization }
            : {}),
          plan: {
            periods,
            candidates: candidates.map((candidate, index) => ({
              id: candidate.id,
              name: candidate.name.trim(),
              factors: built[index]!.factors,
            })),
          },
        };
  return { request, periods, earliest, errors: [...new Set(errors)] };
}
