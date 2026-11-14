import { useEffect, useMemo, useRef } from "react";
import * as echarts from "echarts/core";
import { BarChart, LineChart } from "echarts/charts";
import {
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  TooltipComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import type { EChartsOption } from "echarts";
import { useChartPalette, type ChartPalette } from "../app/theme";
import type {
  AnnualReturn,
  EquityPoint,
  FactorDescriptor,
  FactorHistoryItem,
} from "../types";
import { factorLabel, formatNumber, formatPercent } from "../utils/format";

echarts.use([
  LineChart,
  BarChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomComponent,
  MarkLineComponent,
  CanvasRenderer,
]);

interface ChartProps {
  build: (palette: ChartPalette) => EChartsOption;
  deps: readonly unknown[];
  height: number;
  ariaLabel: string;
  group?: string;
  onDateClick?: (date: string) => void;
  onDataClick?: (data: unknown) => void;
}

/** ECharts host. Options are rebuilt from the theme palette, never patched afterwards. */
export function Chart({
  build,
  deps,
  height,
  ariaLabel,
  group,
  onDateClick,
  onDataClick,
}: ChartProps) {
  const palette = useChartPalette();
  const element = useRef<HTMLDivElement | null>(null);
  const chart = useRef<ReturnType<typeof echarts.init> | null>(null);
  const click = useRef(onDateClick);
  click.current = onDateClick;
  const dataClick = useRef(onDataClick);
  dataClick.current = onDataClick;
  const option = useMemo(() => build(palette), [palette, ...deps]);

  useEffect(() => {
    if (!element.current) return;
    const instance = echarts.init(element.current, undefined, {
      renderer: "canvas",
    });
    chart.current = instance;
    if (group) {
      instance.group = group;
      echarts.connect(group);
    }
    instance.on("click", (params) => {
      dataClick.current?.(params.data);
      if (
        typeof params.name === "string" &&
        /^\d{4}-\d{2}-\d{2}$/.test(params.name)
      )
        click.current?.(params.name);
    });
    const observer = new ResizeObserver(() => instance.resize());
    observer.observe(element.current);
    return () => {
      observer.disconnect();
      instance.dispose();
      chart.current = null;
    };
  }, [group]);

  useEffect(() => {
    const instance = chart.current;
    if (!instance) return;
    // Keep the user's zoom window when only colours change.
    const zoom =
      (instance.getOption()?.dataZoom as Array<{
        start?: number;
        end?: number;
      }>) ?? [];
    const next = { ...option };
    if (Array.isArray(next.dataZoom) && zoom.length) {
      next.dataZoom = next.dataZoom.map((item, index) => ({
        ...item,
        start: zoom[index]?.start ?? item.start,
        end: zoom[index]?.end ?? item.end,
      }));
    }
    instance.setOption(next, { notMerge: true });
  }, [option]);

  return (
    <div
      ref={element}
      className="chart"
      style={{ height }}
      role="img"
      aria-label={ariaLabel}
    />
  );
}

function base(p: ChartPalette): EChartsOption {
  return {
    backgroundColor: "transparent",
    textStyle: { color: p.text, fontFamily: "inherit" },
    animationDuration: 280,
    tooltip: {
      trigger: "axis",
      backgroundColor: p.surface,
      borderColor: p.border,
      textStyle: { color: p.text },
    },
  };
}

function axis(p: ChartPalette, data: string[]) {
  return {
    type: "category" as const,
    data,
    boundaryGap: false,
    axisLine: { lineStyle: { color: p.border } },
    axisTick: { show: false },
    axisLabel: { color: p.muted, fontSize: 11 },
  };
}

function valueAxis(p: ChartPalette, extra: Record<string, unknown> = {}) {
  return {
    type: "value" as const,
    scale: true,
    splitLine: { lineStyle: { color: p.split } },
    axisLabel: { color: p.muted, fontSize: 11 },
    nameTextStyle: { color: p.muted, fontSize: 11 },
    ...extra,
  };
}

const finite = (value: unknown) =>
  typeof value === "number" && Number.isFinite(value) ? value : null;

export function EquityChart({
  items,
  group,
  onDateClick,
}: {
  items: EquityPoint[];
  group?: string;
  onDateClick?: (date: string) => void;
  onDataClick?: (data: unknown) => void;
}) {
  return (
    <Chart
      height={360}
      group={group}
      onDateClick={onDateClick}
      ariaLabel="策略净值与基准净值走势，点击某日查看当日持仓"
      deps={[items]}
      build={(p) => ({
        ...base(p),
        color: [p.primary, p.benchmark, p.accent],
        grid: { left: 8, right: 16, top: 40, bottom: 56, containLabel: true },
        legend: { top: 0, right: 0, textStyle: { color: p.muted } },
        tooltip: {
          ...(base(p).tooltip as object),
          valueFormatter: (value) =>
            finite(Number(value)) === null
              ? "—"
              : formatNumber(Number(value), 4),
        },
        dataZoom: [
          { type: "inside" },
          {
            type: "slider",
            height: 16,
            bottom: 8,
            borderColor: "transparent",
            fillerColor: p.primarySoft,
            textStyle: { color: p.muted, fontSize: 10 },
          },
        ],
        xAxis: axis(
          p,
          items.map((row) => row.date),
        ),
        yAxis: valueAxis(p, { name: "净值" }),
        series: [
          {
            name: "策略净值",
            type: "line",
            showSymbol: false,
            lineStyle: { width: 2.2 },
            areaStyle: { color: p.primarySoft },
            data: items.map((row) => finite(row.equity)),
          },
          {
            name: "基准净值",
            type: "line",
            showSymbol: false,
            lineStyle: { width: 1.6, type: "dashed" },
            data: items.map((row) => finite(row.benchmark)),
          },
          {
            name: "相对净值",
            type: "line",
            showSymbol: false,
            lineStyle: { width: 1.4, type: "dotted" },
            data: items.map((row) =>
              finite(row.equity) !== null &&
              finite(row.benchmark) !== null &&
              (row.benchmark as number) > 0
                ? (row.equity as number) / (row.benchmark as number)
                : null,
            ),
          },
        ],
      })}
    />
  );
}

export function DrawdownChart({
  items,
  group,
}: {
  items: EquityPoint[];
  group?: string;
}) {
  return (
    <Chart
      height={180}
      group={group}
      ariaLabel="策略回撤走势，单位为百分比"
      deps={[items]}
      build={(p) => ({
        ...base(p),
        color: [p.down],
        grid: { left: 8, right: 16, top: 24, bottom: 16, containLabel: true },
        tooltip: {
          ...(base(p).tooltip as object),
          valueFormatter: (value) =>
            finite(Number(value)) === null
              ? "—"
              : `${Number(value).toFixed(2)}%`,
        },
        dataZoom: [{ type: "inside" }],
        xAxis: axis(
          p,
          items.map((row) => row.date),
        ),
        yAxis: valueAxis(p, {
          max: 0,
          scale: false,
          name: "回撤",
          axisLabel: { color: p.muted, formatter: "{value}%" },
        }),
        series: [
          {
            name: "回撤",
            type: "line",
            showSymbol: false,
            lineStyle: { width: 1.4 },
            areaStyle: { opacity: 0.18 },
            data: items.map((row) =>
              finite(row.drawdown) === null
                ? null
                : Math.round((row.drawdown as number) * 10000) / 100,
            ),
          },
        ],
      })}
    />
  );
}

export function AnnualReturnsChart({ items }: { items: AnnualReturn[] }) {
  return (
    <Chart
      height={260}
      ariaLabel="各年度策略与基准收益"
      deps={[items]}
      build={(p) => ({
        ...base(p),
        grid: { left: 8, right: 16, top: 36, bottom: 8, containLabel: true },
        legend: { top: 0, textStyle: { color: p.muted } },
        tooltip: {
          ...(base(p).tooltip as object),
          valueFormatter: (value) =>
            typeof value === "number" ? formatPercent(value) : "—",
        },
        xAxis: {
          ...axis(
            p,
            items.map((row) => String(row.year)),
          ),
          boundaryGap: true,
        },
        yAxis: valueAxis(p, {
          scale: false,
          axisLabel: {
            color: p.muted,
            formatter: (v: number) => formatPercent(v, 0),
          },
        }),
        series: [
          {
            name: "策略",
            type: "bar",
            barMaxWidth: 36,
            data: items.map((row) => ({
              value: row.strategy_return,
              itemStyle: {
                color: (row.strategy_return ?? 0) >= 0 ? p.up : p.down,
              },
            })),
          },
          {
            name: "基准",
            type: "bar",
            barMaxWidth: 36,
            itemStyle: { color: p.benchmark },
            data: items.map((row) => row.benchmark_return),
          },
        ],
      })}
    />
  );
}

export function FactorHistoryChart({
  items,
  definitions,
  percentile,
}: {
  items: FactorHistoryItem[];
  definitions?: Record<string, FactorDescriptor>;
  percentile: boolean;
}) {
  const keys = useMemo(() => {
    const found = new Set<string>();
    for (const row of items)
      for (const [key, value] of Object.entries(row))
        if (key !== "date" && key !== "run_id" && typeof value === "number")
          found.add(key);
    return [...found].sort((a, b) =>
      a === "composite" ? 1 : b === "composite" ? -1 : a.localeCompare(b),
    );
  }, [items]);
  return (
    <Chart
      height={300}
      ariaLabel="个股因子得分历史走势"
      deps={[items, keys, definitions, percentile]}
      build={(p) => ({
        ...base(p),
        color: p.series,
        grid: { left: 8, right: 16, top: 40, bottom: 16, containLabel: true },
        legend: {
          top: 0,
          right: 0,
          type: "scroll",
          textStyle: { color: p.muted },
        },
        tooltip: {
          ...(base(p).tooltip as object),
          valueFormatter: (value) =>
            finite(Number(value)) === null
              ? "—"
              : formatNumber(Number(value), 2),
        },
        xAxis: axis(
          p,
          items.map((row) => row.date),
        ),
        yAxis: valueAxis(
          p,
          percentile ? { min: 0, max: 100, scale: false } : {},
        ),
        series: keys.map((key) => ({
          name: key === "composite" ? "综合分" : factorLabel(key, definitions),
          type: "line" as const,
          showSymbol: false,
          emphasis: { focus: "series" as const },
          lineStyle: { width: key === "composite" ? 2.8 : 1.6 },
          data: items.map((row) => finite(row[key])),
        })),
      })}
    />
  );
}

export function GroupReturnsChart({
  groups,
}: {
  groups: Array<{ group: number; mean_return: number | null }>;
}) {
  return (
    <Chart
      height={200}
      ariaLabel="因子分组平均收益"
      deps={[groups]}
      build={(p) => ({
        ...base(p),
        grid: { left: 8, right: 16, top: 16, bottom: 8, containLabel: true },
        tooltip: {
          ...(base(p).tooltip as object),
          valueFormatter: (value) =>
            typeof value === "number" ? formatPercent(value) : "—",
        },
        xAxis: {
          ...axis(
            p,
            groups.map((row) => `Q${row.group}`),
          ),
          boundaryGap: true,
        },
        yAxis: valueAxis(p, {
          scale: false,
          axisLabel: {
            color: p.muted,
            formatter: (v: number) => formatPercent(v, 1),
          },
        }),
        series: [
          {
            name: "平均收益",
            type: "bar",
            barMaxWidth: 40,
            data: groups.map((row) => ({
              value: row.mean_return,
              itemStyle: { color: (row.mean_return ?? 0) >= 0 ? p.up : p.down },
            })),
          },
        ],
      })}
    />
  );
}
