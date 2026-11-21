import type { ResearchTask } from "../api/research";

export const TASK_STAGE_LABELS: Record<string, string> = {
  prepare: "准备数据",
  factors: "计算因子",
  backtest: "回测",
  evaluation: "因子评估",
  experiment: "方案比较",
  download: "下载数据",
};

const COLLAPSE_THRESHOLD = 72;

export interface TaskFailureDisplay {
  isEmpty: boolean;
  stageLabel: string | null;
  conclusion: string;
  headline: string;
  exitCodeText: string | null;
  fullText: string;
  isLong: boolean;
  previewText: string;
  tone: "danger" | "neutral";
}

function parseExitCode(task: ResearchTask, raw: string): number | null {
  const detailCode = task.error_details?.exit_code;
  if (typeof detailCode === "number" && Number.isFinite(detailCode)) {
    return detailCode;
  }
  const matched = /(?:任务进程退出，代码|（退出码|退出码)\s*(-?\d+)/.exec(raw);
  if (!matched) return null;
  const parsed = Number.parseInt(matched[1], 10);
  return Number.isFinite(parsed) ? parsed : null;
}

function resolveStageKey(task: ResearchTask): string | null {
  const detailStage = task.error_details?.stage;
  if (detailStage && detailStage in TASK_STAGE_LABELS) return detailStage;
  if (task.stage && task.stage in TASK_STAGE_LABELS) return task.stage;
  return null;
}

export function describeTaskFailure(
  task: ResearchTask,
): TaskFailureDisplay | null {
  const isTerminalIssue =
    task.status === "failed" || task.status === "interrupted";
  if (!isTerminalIssue) return null;

  const raw = (task.error ?? task.error_details?.summary ?? "").trim();
  if (!raw) {
    return {
      isEmpty: true,
      stageLabel: null,
      conclusion: "未记录原因",
      headline: "未记录原因",
      exitCodeText: null,
      fullText: "未记录原因",
      isLong: false,
      previewText: "未记录原因",
      tone: "neutral",
    };
  }

  const stageKey = resolveStageKey(task);
  const stageWord = stageKey ? TASK_STAGE_LABELS[stageKey] : null;
  const stageLabel = stageWord ? `${stageWord}阶段` : null;
  const exitCode = parseExitCode(task, raw);

  if (exitCode !== null) {
    let conclusion: string;
    let tone: "danger" | "neutral";
    if (exitCode === -15 || exitCode === 143) {
      conclusion = "被要求停止";
      tone = "neutral";
    } else if (exitCode === -9 || exitCode === 137) {
      conclusion =
        task.error_details?.oom_killed === true
          ? "因内存不足被系统终止"
          : "被系统强制终止";
      tone = task.status === "interrupted" ? "neutral" : "danger";
    } else {
      conclusion = "异常退出";
      tone = task.status === "interrupted" ? "neutral" : "danger";
    }
    const headline = stageLabel ? `${stageLabel} · ${conclusion}` : conclusion;
    const exitCodeText = `退出码 ${exitCode}`;
    const fullText = `${headline} · ${exitCodeText}`;
    return {
      isEmpty: false,
      stageLabel,
      conclusion,
      headline,
      exitCodeText,
      fullText,
      isLong: false,
      previewText: headline,
      tone,
    };
  }

  const conclusion =
    stageLabel && stageWord && !raw.startsWith(stageWord)
      ? `${stageLabel} · ${raw}`
      : raw;
  const normalized = conclusion.replace(/\s+/g, " ").trim();
  const chars = Array.from(normalized);
  const isLong = chars.length > COLLAPSE_THRESHOLD || raw.includes("\n");
  const previewText = isLong
    ? `${chars.slice(0, COLLAPSE_THRESHOLD).join("")}…`
    : normalized;
  return {
    isEmpty: false,
    stageLabel,
    conclusion: raw,
    headline: normalized,
    exitCodeText: null,
    fullText: normalized,
    isLong,
    previewText,
    tone: task.status === "interrupted" ? "neutral" : "danger",
  };
}
