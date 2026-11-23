import { useMemo, useState } from "react";
import { Alert, Button, Segmented } from "antd";
import { PlusCircleOutlined, ReloadOutlined } from "@ant-design/icons";
import { ApiError } from "../api/http";
import { researchApi, type ResearchTask } from "../api/research";
import { navigate, paths } from "../app/routes";
import { TaskDetail, TaskTable } from "../components/tasks";
import {
  EmptyState,
  Loading,
  PageHeader,
  Resource,
  Section,
  StatusTag,
} from "../components/ui";
import { useApiResource } from "../hooks/useApiResource";

type Filter = "all" | "active" | "succeeded" | "failed";
const TASK_POLL_MS = 3000;

const MATCH: Record<Filter, (task: ResearchTask) => boolean> = {
  all: () => true,
  active: (task) => task.status === "queued" || task.status === "running",
  succeeded: (task) => task.status === "succeeded",
  failed: (task) =>
    ["failed", "interrupted", "cancelled"].includes(task.status),
};

export default function TasksPage({ focus }: { focus: string | null }) {
  const tasks = useApiResource((signal) => researchApi.tasks(signal), [], {
    pollMs: TASK_POLL_MS,
  });
  const [filter, setFilter] = useState<Filter>("all");
  const listed = tasks.data?.items.some((task) => task.id === focus);
  const lookup = focus && !listed && (tasks.data || tasks.error) ? focus : null;
  const focused = useApiResource(
    lookup
      ? async (signal) => {
          try {
            return { task: await researchApi.task(lookup, signal) };
          } catch (error) {
            if (
              error instanceof ApiError &&
              (error.status === 403 || error.status === 404)
            ) {
              // Replace the cached task so a later transient failure cannot reveal it again.
              return { task: null };
            }
            throw error;
          }
        }
      : null,
    [lookup],
    { pollMs: TASK_POLL_MS },
  );
  const focusedTask = focused.data?.task;
  function refresh() {
    tasks.reload();
    if (lookup) focused.reload();
  }
  const items = useMemo(
    () => (tasks.data?.items ?? []).filter(MATCH[filter]),
    [tasks.data, filter],
  );
  const counts = useMemo(() => {
    const all = tasks.data?.items ?? [];
    return Object.fromEntries(
      (Object.keys(MATCH) as Filter[]).map((key) => [
        key,
        all.filter(MATCH[key]).length,
      ]),
    ) as Record<Filter, number>;
  }, [tasks.data]);

  return (
    <>
      <PageHeader
        title="任务中心"
        subtitle="研究与方案比较任务在后台工作进程中依次运行，本页每 3 秒自动刷新"
        extra={
          <>
            <Button
              icon={<ReloadOutlined />}
              loading={tasks.refreshing || focused.refreshing}
              onClick={refresh}
            >
              刷新
            </Button>
            <Button
              type="primary"
              icon={<PlusCircleOutlined />}
              onClick={() => navigate(paths.newResearch())}
            >
              新建研究
            </Button>
          </>
        }
      />
      {lookup ? (
        <Section title="指定任务">
          {focusedTask &&
          (focusedTask.kind === "research" ||
            focusedTask.kind === "experiment" ||
            !focusedTask.kind) ? (
            <>
              <StatusTag status={focusedTask.status} />
              <TaskDetail task={focusedTask} />
            </>
          ) : focused.data ? (
            <EmptyState title="任务不可用" />
          ) : focused.error ? (
            <Alert
              type="warning"
              title="任务加载失败"
              description={focused.error}
              action={
                <Button onClick={focused.reload} loading={focused.refreshing}>
                  重试
                </Button>
              }
            />
          ) : (
            <Loading rows={2} />
          )}
        </Section>
      ) : null}
      <Section
        extra={
          <div className="task-filter-wrap">
            <Segmented<Filter>
              value={filter}
              onChange={setFilter}
              options={[
                { value: "all", label: `全部 ${counts.all}` },
                { value: "active", label: `进行中 ${counts.active}` },
                { value: "succeeded", label: `已完成 ${counts.succeeded}` },
                { value: "failed", label: `失败/中断 ${counts.failed}` },
              ]}
            />
          </div>
        }
        title="任务列表"
      >
        <Resource
          state={tasks}
          empty={(data) =>
            data.items.length ? null : (
              <EmptyState
                title="还没有任务"
                description="提交研究或方案比较后，进度和结果会显示在这里。"
                action={
                  <Button
                    type="primary"
                    onClick={() => navigate(paths.newResearch())}
                  >
                    新建研究
                  </Button>
                }
              />
            )
          }
        >
          {() => (
            <TaskTable
              tasks={items}
              onChanged={tasks.reload}
              expandedId={focus}
              resetKey={filter}
            />
          )}
        </Resource>
      </Section>
    </>
  );
}
