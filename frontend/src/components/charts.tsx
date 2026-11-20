import { useEffect, useMemo, useRef, useState } from "react";
import { Typography } from "antd";
import * as echarts from "echarts/core";
import { BarChart, LineChart } from "echarts/charts";
import {
  DataZoomComponent,
  GraphicComponent,
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  MarkPointComponent,
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
  GraphicComponent,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomComponent,
  MarkLineComponent,
  MarkPointComponent,
  CanvasRenderer,
]);

interface ChartProps {
  build: (palette: ChartPalette, width: number) => EChartsOption;
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
  const [width, setWidth] = useState(0);
  const click = useRef(onDateClick);
  click.current = onDateClick;
  const dataClick = useRef(onDataClick);
  dataClick.current = onDataClick;
  const option = useMemo(
    () => build(palette, width),
    [palette, width, ...deps],
  );

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
    const observer = new ResizeObserver(() => {
      instance.resize();
      setWidth(element.current?.clientWidth ?? 0);
    });
    observer.observe(element.current);
    setWidth(element.current.clientWidth);
    return () => {
      observer.disconnect();
      instance.dispose();
      chart.current = null;
    };
  }, [group]);

  useEffect(() => {
    const instance = chart.current;
    if (!instance) return;
    // Keep user state across option rebuilds (theme change, resize): the zoom
    // window and any legend series the reader turned off.
    const previous = instance.getOption();
    const zoom =
      (previous?.dataZoom as Array<{
        start?: number;
        end?: number;
      }>) ?? [];
    const legend =
      (previous?.legend as Array<{
        selected?: Record<string, boolean>;
      }>) ?? [];
    const next = { ...option };
    if (Array.isArray(next.dataZoom) && zoom.length) {
      next.dataZoom = next.dataZoom.map((item, index) => ({
        ...item,
        start: zoom[index]?.start ?? item.start,
        end: zoom[index]?.end ?? item.end,
      }));
    }
    if (Array.isArray(next.legend) && legend.length) {
      next.legend = next.legend.map((item, index) => ({
        ...item,
        selected: { ...item.selected, ...legend[index]?.selected },
      }));
    } else if (next.legend && !Array.isArray(next.legend) && legend.length) {
      next.legend = {
        ...next.legend,
        selected: { ...next.legend.selected, ...legend[0]?.selected },
      };
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

/**
 * Category axis for dated series: both ends keep the full `YYYY-MM-DD` label and
 * are aligned inwards so they stay inside the canvas at any width. Middle ticks
 * are stepped from the measured width, so narrow charts drop them instead of
 * letting labels collide with the endpoints.
 */
const DATE_LABEL_WIDTH = 70;
const DATE_LABEL_GAP = 8;
// An endpoint label is pinned against the plot edge, a middle label is centred:
// keep a full endpoint label, a gap and half a centred label clear of either end.
const DATE_EDGE_CLEARANCE = DATE_LABEL_WIDTH * 1.5 + DATE_LABEL_GAP;

function dateAxis(p: ChartPalette, data: string[], width: number) {
  const count = data.length;
  const plot = Math.max(120, width - 72);
  const slots = Math.max(
    2,
    Math.floor(plot / (DATE_LABEL_WIDTH + DATE_LABEL_GAP)),
  );
  const step = Math.max(1, Math.ceil((count - 1) / (slots - 1)));
  const edge = Math.ceil(((count - 1) * DATE_EDGE_CLEARANCE) / plot);
  const showInterval = (index: number) =>
    index === 0 ||
    index === count - 1 ||
    (index % step === 0 && index >= edge && count - 1 - index >= edge);
  return {
    ...axis(p, data),
    axisLabel: {
      color: p.muted,
      fontSize: 11,
      hideOverlap: true,
      showMinLabel: true,
      showMaxLabel: true,
      alignMinLabel: "left" as const,
      alignMaxLabel: "right" as const,
      interval: (index: number) => showInterval(index),
    },
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
  anchorDate,
  onDateClick,
}: {
  items: EquityPoint[];
  group?: string;
  anchorDate?: string | null;
  onDateClick?: (date: string) => void;
  onDataClick?: (data: unknown) => void;
}) {
  const first = items[0]?.date;
  const last = items[items.length - 1]?.date;
  const range = first && last ? `，区间 ${first} 至 ${last}` : "";
  return (
    <Chart
      height={360}
      group={group}
      onDateClick={onDateClick}
      ariaLabel={`策略净值与基准净值走势${range}，期初基准点仅作起点，点击区间内日期查看持仓`}
      deps={[items, anchorDate]}
      build={(p, width) => ({
        ...base(p),
        color: [p.primary, p.benchmark, p.accent],
        grid: { left: 8, right: 28, top: 40, bottom: 56, containLabel: true },
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
        xAxis: dateAxis(
          p,
          items.map((row) => row.date),
          width,
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
            ...(anchorDate
              ? {
                  markPoint: {
                    symbol: "circle",
                    symbolSize: 7,
                    itemStyle: { color: p.primary },
                    label: {
                      formatter: "期初基准点",
                      color: p.muted,
                      fontSize: 10,
                      position: "bottom",
                    },
                    data: [{ name: "期初基准点", coord: [anchorDate, 1] }],
                  },
                }
              : {}),
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
  const first = items[0]?.date;
  const last = items[items.length - 1]?.date;
  const range = first && last ? `，区间 ${first} 至 ${last}` : "";
  return (
    <Chart
      height={180}
      group={group}
      ariaLabel={`策略回撤走势${range}，单位为百分比`}
      deps={[items]}
      build={(p, width) => ({
        ...base(p),
        color: [p.down],
        grid: { left: 8, right: 28, top: 24, bottom: 16, containLabel: true },
        tooltip: {
          ...(base(p).tooltip as object),
          valueFormatter: (value) =>
            finite(Number(value)) === null
              ? "—"
              : `${Number(value).toFixed(2)}%`,
        },
        dataZoom: [{ type: "inside" }],
        xAxis: dateAxis(
          p,
          items.map((row) => row.date),
          width,
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
  reason,
  onShowCoverage,
}: {
  groups: Array<{ group: number; mean_return: number | null }>;
  /** A cause the stored diagnostics prove; null keeps the generic wording. */
  reason?: string | null;
  onShowCoverage?: () => void;
}) {
  const values = groups.map((row) =>
    typeof row.mean_return === "number" && Number.isFinite(row.mean_return)
      ? row.mean_return
      : null,
  );
  const missing = values.filter((value) => value === null).length;
  const hasValues = values.some((value) => value !== null);
  return (
    <div className="chart-with-note">
      <Chart
        height={200}
        ariaLabel={
          hasValues
            ? "因子分组平均收益"
            : "因子分组平均收益：暂无可展示的数据，缺失的组未按 0 显示"
        }
        deps={[groups]}
        build={(p) => ({
          ...base(p),
          grid: { left: 8, right: 16, top: 16, bottom: 8, containLabel: true },
          // With no finite value the plot keeps its axes and says so inside the
          // drawing area instead of leaving an unexplained blank box.
          graphic: hasValues
            ? undefined
            : [
                {
                  type: "text" as const,
                  left: "center",
                  top: "middle",
                  style: {
                    text: "暂无可展示的分组收益",
                    fill: p.muted,
                    fontSize: 13,
                  },
                },
              ],
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
              data: values.map((value) => ({
                value,
                itemStyle: { color: (value ?? 0) >= 0 ? p.up : p.down },
              })),
            },
          ],
        })}
      />
      {hasValues && missing === 0 ? null : (
        <Typography.Text type="secondary" className="chart-note">
          {hasValues
            ? `有 ${missing} 组缺少有效收益，未按 0 显示。`
            : (reason ?? `共 ${groups.length} 组均无有效收益，未按 0 显示。`)}
          {!hasValues && reason && onShowCoverage ? (
            <>
              {" "}
              <Typography.Link onClick={onShowCoverage}>
                查看覆盖信息
              </Typography.Link>
            </>
          ) : null}
        </Typography.Text>
      )}
    </div>
  );
}
