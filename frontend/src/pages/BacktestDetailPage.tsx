import { useState, type FocusEvent } from "react";
import {
  Button,
  Card,
  Descriptions,
  Flex,
  Statistic,
  Tabs,
  Tag,
  Typography,
} from "antd";
import { DownloadOutlined } from "@ant-design/icons";
import { api } from "../api/client";
import { navigate, paths } from "../app/routes";
import { TemporalAuditPanel } from "../components/audit";
import { UniversePanel } from "../components/UniversePanel";
import {
  CostsPanel,
  EventsPanel,
  IndustryPanel,
  PerformancePanel,
  PositionsPanel,
  TradesPanel,
} from "../components/backtest";
import { DrawdownChart, EquityChart } from "../components/charts";
import {
  EmptyState,
  ErrorState,
  Loading,
  MonoId,
  Notes,
  PageHeader,
  Section,
  StatusTag,
} from "../components/ui";
import { useApiResource } from "../hooks/useApiResource";
import type { BacktestDetail } from "../types";
import { positionDates } from "./backtestDates";
import {
  formatDateTime,
  formatMetricValue,
  metricLabel,
  REBALANCE_LABELS,
  SIGNED_METRICS,
  toTextList,
} from "../utils/format";

const KPIS = [
  "total_return",
  "annualized_return",
  "excess_return",
  "max_drawdown",
  "sharpe_ratio",
  "total_cost_amount",
];

export default function BacktestDetailPage({
  id,
  tab,
}: {
  id: string;
  tab: string;
}) {
  const detail = useApiResource((signal) => api.backtest(id, signal), [id]);
  const equity = useApiResource((signal) => api.equity(id, signal), [id]);
  const [positionDate, setPositionDate] = useState<string | null>(null);

  if (detail.data === null)
    return detail.error ? (
      <>
        <PageHeader
          title="回测"
          back={{ to: paths.backtests(), label: "全部回测" }}
        />
        <ErrorState message={detail.error} onRetry={detail.reload} />
      </>
    ) : (
      <Loading rows={10} />
    );

  const data = detail.data;
  const dates = positionDates(equity.data?.items ?? [], data);
  const dynamic = [
    "HistoricalIndexTopkStrategy",
    "DynamicUniverseTopkStrategy",
  ].includes(data.strategy);
  const go = (next: string) =>
    navigate(paths.backtest(id, next === "overview" ? undefined : next), {
      replace: true,
    });
  const openPositions = (date: string) => {
    setPositionDate(date);
    go("positions");
  };

  const items = [
    {
      key: "overview",
      label: "概览",
      children: (
        <Flex vertical gap={16}>
          <Section
            title="净值与回撤"
            description="期初基准点仅作起点，点击区间内日期查看当日持仓"
          >
            {equity.data === null ? (
              equity.error ? (
                <ErrorState message={equity.error} onRetry={equity.reload} />
              ) : (
                <Loading />
              )
            ) : equity.data.items.length ? (
              <>
                <EquityChart
                  items={equity.data.items}
                  group={id}
                  anchorDate={
                    equity.data.items[0]?.date < data.start_date
                      ? equity.data.items[0].date
                      : null
                  }
                  onDateClick={(date) =>
                    dates.includes(date) && openPositions(date)
                  }
                />
                <DrawdownChart items={equity.data.items} group={id} />
              </>
            ) : (
              <EmptyState title="没有净值序列" />
            )}
          </Section>
          <PerformancePanel detail={data} />
        </Flex>
      ),
    },
    {
      key: "positions",
      label: "持仓",
      children: (
        <PositionsPanel
          runId={id}
          dates={dates}
          date={positionDate}
          onDate={setPositionDate}
        />
      ),
    },
    ...(data.universe_filters
      ? [
          {
            key: "universe",
            label: "股票池筛选",
            children: <UniversePanel detail={data} />,
          },
        ]
      : []),
    {
      key: "trades",
      label: "成交",
      children: (
        <TradesPanel runId={id} costMode={data.execution_costs?.cost_mode} />
      ),
    },
    {
      key: "costs",
      label: "费用与调仓",
      children: <CostsPanel detail={data} />,
    },
    {
      key: "industry",
      label: "行业暴露",
      children: (
        <IndustryPanel kind="backtests" runId={id} date={positionDate} />
      ),
    },
    ...(dynamic
      ? [
          {
            key: "events",
            label: "执行记录",
            children: (
              <EventsPanel runId={id} summary={data.execution_summary} />
            ),
          },
        ]
      : []),
    {
      key: "settings",
      label: "参数与口径",
      children: <SettingsPanel detail={data} />,
    },
  ];

  return (
    <>
      <PageHeader
        back={{ to: paths.backtests(), label: "全部回测" }}
        title={data.name}
        tags={
          <>
            <StatusTag status={data.status} />
            <Tag>
              {data.universe_filters
                ? "动态条件股票池"
                : dynamic
                  ? "历史成分动态池"
                  : "固定股票池"}
            </Tag>
            {data.is_synthetic ? <Tag color="warning">合成样本</Tag> : null}
          </>
        }
        subtitle={`${data.start_date} ~ ${data.end_date} · ${REBALANCE_LABELS[data.rebalance_policy?.frequency ?? "daily"]}调仓 · 创建于 ${formatDateTime(data.created_at)}`}
        extra={
          <Button
            icon={<DownloadOutlined />}
            href={api.backtestDownloadUrl(id)}
            download={`backtest-${id}.json`}
          >
            下载 JSON
          </Button>
        }
      />
      <Card className="kpi-card">
        <div className="kpi-grid">
          {KPIS.map((key) => {
            const value = data.metrics?.[key];
            const signed =
              SIGNED_METRICS.has(key) &&
              typeof value === "number" &&
              value !== 0;
            return (
              <Statistic
                key={key}
                title={metricLabel(key)}
                value={formatMetricValue(key, value)}
                styles={{
                  content: signed
                    ? {
                        color:
                          value > 0 ? "var(--quant-up)" : "var(--quant-down)",
                      }
                    : undefined,
                }}
              />
            );
          })}
        </div>
      </Card>
      <div
        className="result-tabs-host"
        onFocus={(event: FocusEvent<HTMLDivElement>) => {
          // Keep a keyboard-focused tab inside the scrollable strip: at 200%
          // zoom the later tabs are otherwise focused off-screen.
          const target = event.target as HTMLElement;
          if (!target.classList.contains("ant-tabs-tab-btn")) return;
          // rc-tabs also adjusts the strip while focus moves; run after it.
          requestAnimationFrame(() => {
            const strip = target.closest(".ant-tabs-nav-wrap");
            if (!strip) return;
            const tab = target.getBoundingClientRect();
            const view = strip.getBoundingClientRect();
            if (tab.left < view.left)
              strip.scrollLeft -= view.left - tab.left + 8;
            else if (tab.right > view.right)
              strip.scrollLeft += tab.right - view.right + 8;
          });
        }}
      >
        <Tabs
          className="result-tabs"
          activeKey={items.some((item) => item.key === tab) ? tab : "overview"}
          onChange={go}
          items={items}
        />
      </div>
    </>
  );
}

function SettingsPanel({ detail }: { detail: BacktestDetail }) {
  const config = detail.config ?? {};
  const limitations = [
    ...toTextList(detail.limitations),
    ...toTextList(
      (detail as unknown as { position_note?: unknown }).position_note,
    ),
  ];
  return (
    <Flex vertical gap={16}>
      <Section title="运行信息">
        <Descriptions
          size="small"
          column={{ xs: 1, md: 2 }}
          items={[
            {
              key: "id",
              label: "回测编号",
              children: <MonoId value={detail.id} />,
            },
            {
              key: "data",
              label: "数据版本",
              children: <MonoId value={detail.data_version} />,
            },
            {
              key: "dataset",
              label: "冻结数据集",
              children: <MonoId value={detail.dataset_id} />,
            },
            {
              key: "factor",
              label: "因子批次",
              children:
                typeof config.factor_run_id === "string" ? (
                  <MonoId value={config.factor_run_id} />
                ) : (
                  "—"
                ),
            },
            {
              key: "engine",
              label: "引擎",
              children: `${detail.engine ?? "qlib"} ${detail.engine_version ?? ""}`,
            },
            { key: "strategy", label: "策略", children: detail.strategy },
          ]}
        />
      </Section>
      <Section title="运行参数">
        <Descriptions
          size="small"
          column={{ xs: 1, md: 2 }}
          items={Object.entries(config).map(([key, value]) => ({
            key,
            label: key,
            children: (
              <Typography.Text style={{ fontSize: 12, whiteSpace: "pre-wrap" }}>
                {typeof value === "object"
                  ? JSON.stringify(value)
                  : String(value)}
              </Typography.Text>
            ),
          }))}
        />
      </Section>
      {limitations.length ? (
        <Section title="已知限制">
          <Notes defaultOpen title="展开全部限制" items={limitations} />
        </Section>
      ) : null}
      <TemporalAuditPanel audit={detail.temporal_audit} />
    </Flex>
  );
}
