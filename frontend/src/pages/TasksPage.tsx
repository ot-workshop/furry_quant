import { useMemo, useState } from "react";
import { Button, Segmented } from "antd";
import { PlusCircleOutlined, ReloadOutlined } from "@ant-design/icons";
import { researchApi, type ResearchTask } from "../api/research";
import { navigate, paths } from "../app/routes";
import { TaskTable } from "../components/tasks";
import { EmptyState, PageHeader, Resource, Section } from "../components/ui";
import { useApiResource } from "../hooks/useApiResource";

type Filter = "all" | "active" | "succeeded" | "failed";

const MATCH: Record<Filter, (task: ResearchTask) => boolean> = {
  all: () => true,
  active: (task) => task.status === "queued" || task.status === "running",
  succeeded: (task) => task.status === "succeeded",
  failed: (task) =>
    ["failed", "interrupted", "cancelled"].includes(task.status),
};

export default function TasksPage({ focus }: { focus: string | null }) {
  const tasks = useApiResource((signal) => researchApi.tasks(signal), [], {
    pollMs: 3000,
  });
  const [filter, setFilter] = useState<Filter>("all");
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
              loading={tasks.refreshing}
              onClick={tasks.reload}
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
            />
          )}
        </Resource>
      </Section>
    </>
  );
}
