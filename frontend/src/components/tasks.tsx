import { useEffect, useState } from "react";
import { poolLabel } from "../utils/format";
import {
  App,
  Button,
  Descriptions,
  Flex,
  Pagination,
  Popconfirm,
  Progress,
  Space,
  Steps,
  Tag,
  Typography,
} from "antd";
import {
  isExperimentTask,
  researchApi,
  type ResearchRequest,
  type ResearchTask,
} from "../api/research";
import { describeError } from "../api/http";
import { navigate, paths } from "../app/routes";
import {
  factorLabel,
  formatDateTime,
  formatDuration,
  REBALANCE_LABELS,
} from "../utils/format";
import { describeTaskFailure } from "../utils/taskFailure";
import { AppLink, StatusTag } from "./ui";

const RESEARCH_STAGES = [
  { key: "prepare", title: "准备数据" },
  { key: "factors", title: "计算因子" },
  { key: "backtest", title: "回测" },
  { key: "evaluation", title: "因子评估" },
];

function stageIndex(task: ResearchTask): number {
  if (task.status === "succeeded") return RESEARCH_STAGES.length;
  const index = RESEARCH_STAGES.findIndex((stage) => stage.key === task.stage);
  return index < 0
    ? Math.min(task.progress.completed, RESEARCH_STAGES.length - 1)
    : index;
}

function percent(task: ResearchTask) {
  const { completed, total } = task.progress;
  if (task.status === "succeeded") return 100;
  return total ? Math.round((completed / total) * 100) : 0;
}

export function TaskProgress({ task }: { task: ResearchTask }) {
  if (task.status === "queued")
    return <Typography.Text type="secondary">等待运行</Typography.Text>;
  const failed = task.status === "failed" || task.status === "interrupted";
  return (
    <Flex vertical gap={2} style={{ minWidth: 160 }}>
      <Progress
        percent={percent(task)}
        size="small"
        status={
          failed
            ? "exception"
            : task.status === "succeeded"
              ? "success"
              : "active"
        }
        showInfo={false}
      />
      <Typography.Text type="secondary" style={{ fontSize: 12 }} ellipsis>
        {task.message}
      </Typography.Text>
    </Flex>
  );
}

function ResultLinks({ task }: { task: ResearchTask }) {
  const result = task.result;
  if (task.status !== "succeeded" || !result) return null;
  if (result.experiment_run_id)
    return (
      <AppLink to={paths.experiment(result.experiment_run_id)}>
        查看方案比较
      </AppLink>
    );
  return (
    <Space size={12} wrap>
      {result.backtest_run_id ? (
        <AppLink to={paths.backtest(result.backtest_run_id)}>回测</AppLink>
      ) : null}
      {result.factor_run_id ? (
        <AppLink
          to={paths.factors({
            run: result.factor_run_id,
            date:
              result.factor_as_of ??
              (task.request as ResearchRequest).backtest?.end,
          })}
        >
          因子评分
        </AppLink>
      ) : null}
      {result.evaluation_run_id ? (
        <AppLink to={paths.evaluation(result.evaluation_run_id)}>
          因子评估
        </AppLink>
      ) : null}
    </Space>
  );
}

function RequestSummary({ task }: { task: ResearchTask }) {
  if (isExperimentTask(task)) {
    const request = task.request;
    return (
      <Descriptions
        size="small"
        column={{ xs: 1, md: 2 }}
        items={[
          {
            key: "codes",
            label: "股票池",
            children: poolLabel(request.data),
          },
          {
            key: "periods",
            label: request.plan.comparison ? "对照区间" : "分段",
            children: Object.values(request.plan.periods)
              .map((period) => `${period.start} ~ ${period.end}`)
              .join(" / "),
          },
          {
            key: "candidates",
            label: "候选方案",
            children: request.plan.candidates.map((c) => c.name).join("、"),
          },
          {
            key: "topk",
            label: "目标持股",
            children: `${request.backtest.topk} 只`,
          },
        ]}
      />
    );
  }
  const request = task.request as ResearchRequest;
  return (
    <Descriptions
      size="small"
      column={{ xs: 1, md: 2 }}
      items={[
        {
          key: "data",
          label: "数据",
          children:
            request.data.mode === "market_warehouse"
              ? request.data.codes
                ? `自选股票 · ${request.data.codes.length} 只`
                : "全市场（使用所选数据集覆盖范围）"
              : request.data.mode === "index_warehouse"
                ? "沪深300历史成分（动态股票池）"
                : `快照 ${request.data.snapshot_id ?? "—"} · ${request.data.codes.length} 只`,
        },
        {
          key: "filters",
          label: "条件筛选",
          children: request.data.filters
            ? `开启 · 排除ST${request.data.filters.exclude_st ? "开启" : "关闭"} · 上市满${request.data.filters.min_listing_days}天`
            : "关闭",
        },
        {
          key: "range",
          label: "回测区间",
          children: `${request.backtest.start} ~ ${request.backtest.end}`,
        },
        {
          key: "factors",
          label: "因子",
          children: request.factors
            .map(
              (f) =>
                `${factorLabel(f.name)}${f.window ? `(${f.window}日)` : ""} ${(f.weight * 100).toFixed(0)}%`,
            )
            .join("，"),
        },
        {
          key: "portfolio",
          label: "组合",
          children: `持股 ${request.backtest.topk} 只 · ${REBALANCE_LABELS[request.backtest.rebalance_frequency ?? "daily"]}调仓 · 资金 ${request.backtest.initial_cash.toLocaleString("zh-CN")} 元`,
        },
        {
          key: "neutral",
          label: "行业中性化",
          children:
            request.neutralization?.mode === "industry" ? "开启" : "关闭",
        },
        {
          key: "benchmark",
          label: "评价基准",
          children: request.backtest.benchmark ?? "沿用数据集基准",
        },
      ]}
    />
  );
}

export function TaskDetail({ task }: { task: ResearchTask }) {
  const failed = task.status === "failed" || task.status === "interrupted";
  const failure = describeTaskFailure(task);
  return (
    <Flex vertical gap={12} className="task-detail-panel">
      {!isExperimentTask(task) &&
      task.status !== "queued" &&
      task.status !== "cancelled" ? (
        <Steps
          size="small"
          current={stageIndex(task)}
          status={
            failed
              ? "error"
              : task.status === "succeeded"
                ? "finish"
                : "process"
          }
          items={RESEARCH_STAGES.map((stage) => ({ title: stage.title }))}
        />
      ) : null}
      {failure ? (
        <Typography.Text
          type={failure.tone === "neutral" ? "secondary" : undefined}
          className="task-detail-reason"
        >
          <span className="task-reason-label">
            {task.status === "interrupted" ? "已中断" : "失败原因"}
          </span>{" "}
          {failure.fullText}
        </Typography.Text>
      ) : null}
      {task.error_details?.trace?.length ? (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          失败位置：
          {task.error_details.trace
            .slice(-3)
            .map((frame) => `${frame.file}:${frame.line} ${frame.function}`)
            .join(" → ")}
        </Typography.Text>
      ) : null}
      <RequestSummary task={task} />
    </Flex>
  );
}

const PAGE_SIZE = 20;

export function TaskTable({
  tasks,
  onChanged,
  compact = false,
  expandedId,
}: {
  tasks: ResearchTask[];
  onChanged: () => void;
  compact?: boolean;
  expandedId?: string | null;
}) {
  const { message } = App.useApp();
  const [page, setPage] = useState(1);
  const [expandedDetails, setExpandedDetails] = useState<
    Record<string, boolean>
  >(() => (expandedId ? { [expandedId]: true } : {}));
  const [expandedReasons, setExpandedReasons] = useState<
    Record<string, boolean>
  >({});

  useEffect(() => {
    if (expandedId) {
      setExpandedDetails((prev) =>
        prev[expandedId] ? prev : { ...prev, [expandedId]: true },
      );
    }
  }, [expandedId]);

  const maxPage = Math.max(1, Math.ceil(tasks.length / PAGE_SIZE));
  const activePage = Math.min(page, maxPage);
  const visibleTasks = compact
    ? tasks
    : tasks.slice((activePage - 1) * PAGE_SIZE, activePage * PAGE_SIZE);

  async function cancel(task: ResearchTask) {
    try {
      await researchApi.cancel(task.id);
      message.success("已取消排队任务");
    } catch (error) {
      message.error(describeError(error));
    } finally {
      onChanged();
    }
  }

  return (
    <div className="task-list-wrap">
      <div className="task-list" role="list">
        {visibleTasks.map((task) => {
          const failure = describeTaskFailure(task);
          const duration = formatDuration(task.started_at, task.finished_at);
          const detailOpen = !compact && Boolean(expandedDetails[task.id]);
          const reasonOpen = Boolean(expandedReasons[task.id]);
          const isFailureAccent =
            task.status === "failed" && failure?.tone !== "neutral";
          const isActive =
            task.status === "queued" || task.status === "running";

          return (
            <div
              key={task.id}
              role="listitem"
              data-task-id={task.id}
              className={`task-row${isFailureAccent ? " task-row-failed" : ""}`}
            >
              <div className="task-row-main">
                <div className="task-row-info">
                  <div className="task-row-head">
                    <Typography.Text strong className="task-row-title">
                      {task.name}
                    </Typography.Text>
                    <StatusTag status={task.status} />
                    {isExperimentTask(task) ? <Tag>方案比较</Tag> : null}
                  </div>
                  {failure ? (
                    <div className="task-row-reason">
                      {failure.isEmpty ? (
                        <span className="task-reason-empty">
                          {failure.fullText}
                        </span>
                      ) : (
                        <>
                          <span className="task-reason-label">原因</span>
                          <span
                            className={`task-reason-body${failure.tone === "neutral" ? " task-reason-neutral" : ""}`}
                          >
                            <span>
                              {failure.isLong && !reasonOpen
                                ? failure.previewText
                                : failure.headline}
                            </span>
                            {failure.exitCodeText ? (
                              <span className="task-reason-code numeric-token">
                                {failure.exitCodeText}
                              </span>
                            ) : null}
                            {failure.isLong ? (
                              <button
                                type="button"
                                className="task-reason-toggle"
                                aria-expanded={reasonOpen}
                                onClick={() =>
                                  setExpandedReasons((prev) => ({
                                    ...prev,
                                    [task.id]: !prev[task.id],
                                  }))
                                }
                              >
                                {reasonOpen ? "收起原因" : "查看完整原因"}
                              </button>
                            ) : null}
                          </span>
                        </>
                      )}
                    </div>
                  ) : null}
                  <div className="task-row-meta">
                    <span className="numeric-token">
                      {formatDateTime(task.created_at)}
                    </span>
                    {!compact && duration !== "—" ? (
                      <span>
                        耗时 <span className="numeric-token">{duration}</span>
                      </span>
                    ) : null}
                    {task.status === "failed" ||
                    task.status === "interrupted" ? (
                      <span className="numeric-token">
                        进度 {task.progress.completed}/{task.progress.total}
                      </span>
                    ) : null}
                    {task.status === "cancelled" ? (
                      <span>{task.message}</span>
                    ) : null}
                    <ResultLinks task={task} />
                  </div>
                  {isActive ? (
                    <div className="task-row-progress">
                      <TaskProgress task={task} />
                    </div>
                  ) : null}
                </div>
                <div className="task-row-actions">
                  {task.status === "queued" ? (
                    <Popconfirm
                      title="取消这个排队任务？"
                      onConfirm={() => void cancel(task)}
                    >
                      <Button size="small" type="link" danger>
                        取消
                      </Button>
                    </Popconfirm>
                  ) : null}
                  {!compact && !isExperimentTask(task) ? (
                    <Button
                      size="small"
                      type="link"
                      onClick={() => navigate(paths.newResearch(task.id))}
                    >
                      复用配置
                    </Button>
                  ) : null}
                  {!compact && task.kind === "research" ? (
                    <Button
                      size="small"
                      type="link"
                      onClick={() =>
                        navigate(
                          `${paths.newExperiment()}?mode=comparison&from=${encodeURIComponent(task.id)}`,
                        )
                      }
                    >
                      收益来源对照
                    </Button>
                  ) : null}
                  {!compact ? (
                    <Button
                      size="small"
                      type="link"
                      aria-expanded={detailOpen}
                      onClick={() =>
                        setExpandedDetails((prev) => ({
                          ...prev,
                          [task.id]: !prev[task.id],
                        }))
                      }
                    >
                      {detailOpen ? "收起详情" : "详情"}
                    </Button>
                  ) : null}
                </div>
              </div>
              {detailOpen ? (
                <div className="task-row-detail">
                  <TaskDetail task={task} />
                </div>
              ) : null}
            </div>
          );
        })}
      </div>
      {!compact && tasks.length > PAGE_SIZE ? (
        <Flex justify="end" style={{ marginTop: 12 }}>
          <Pagination
            current={activePage}
            pageSize={PAGE_SIZE}
            total={tasks.length}
            onChange={setPage}
            showSizeChanger={false}
          />
        </Flex>
      ) : null}
    </div>
  );
}
