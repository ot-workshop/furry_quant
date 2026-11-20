import type { EChartsOption } from "echarts";
import type { ChartPalette } from "../app/theme";
import type { CorrelationCell, RollingICPoint } from "../types";

export type ICChartMetric = "daily" | "mean" | "ir" | "cumulative";

export function icChartOption(
  points: Array<Pick<RollingICPoint, "date"> & Partial<RollingICPoint>>,
  metric: ICChartMetric,
  palette: ChartPalette,
): EChartsOption {
  const fields =
    metric === "mean"
      ? (["ic_mean", "rank_ic_mean"] as const)
      : metric === "ir"
        ? (["ic_ir", "rank_ic_ir"] as const)
        : metric === "cumulative"
          ? (["cumulative_ic", "cumulative_rank_ic"] as const)
          : (["ic", "rank_ic"] as const);
  return {
    backgroundColor: "transparent",
    textStyle: { color: palette.text, fontFamily: "inherit" },
    grid: { left: 58, right: 24, top: 45, bottom: 62 },
    tooltip: {
      trigger: "axis",
      renderMode: "richText",
      backgroundColor: palette.surface,
      textStyle: { color: palette.text },
    },
    legend: { textStyle: { color: palette.text }, top: 0 },
    xAxis: {
      type: "category",
      data: points.map((point) => point.date),
      boundaryGap: false,
      axisLabel: { color: palette.muted },
      axisLine: { lineStyle: { color: palette.border } },
    },
    yAxis: {
      type: "value",
      ...(metric === "daily" || metric === "mean" ? { min: -1, max: 1 } : {}),
      axisLabel: { color: palette.muted },
      splitLine: { lineStyle: { color: palette.split } },
    },
    dataZoom: [
      { type: "inside" },
      {
        type: "slider",
        height: 18,
        bottom: 8,
        borderColor: palette.border,
        textStyle: { color: palette.muted },
      },
    ],
    series: fields.map((field, index) => ({
      name: index ? "Rank IC" : "IC",
      type: "line",
      data: points.map((point) => point[field] ?? null),
      showSymbol: false,
      connectNulls: false,
      smooth: false,
      lineStyle: { width: 1.7 },
      itemStyle: { color: palette.series[index ? 3 : 0] },
      markLine: {
        silent: true,
        symbol: "none",
        data: [{ yAxis: 0 }],
        lineStyle: { color: palette.muted },
        label: { show: false },
      },
    })),
  };
}

export function correlationChartOption(
  names: string[],
  cells: CorrelationCell[],
  labels: Record<string, string>,
  palette: ChartPalette,
): EChartsOption {
  const data = cells
    .filter((cell) => cell.value !== null)
    .flatMap((cell) => {
      const x = names.indexOf(cell.first),
        y = names.indexOf(cell.second);
      const point = {
        value: [x, y, cell.value],
        first: cell.first,
        second: cell.second,
        count: cell.valid_dates,
      };
      return x === y
        ? [point]
        : [
            point,
            {
              ...point,
              value: [y, x, cell.value],
              first: cell.second,
              second: cell.first,
            },
          ];
    });
  return {
    backgroundColor: "transparent",
    textStyle: { color: palette.text, fontFamily: "inherit" },
    grid: { left: 130, right: 35, top: 25, bottom: 110 },
    tooltip: {
      renderMode: "richText",
      backgroundColor: palette.surface,
      textStyle: { color: palette.text },
      formatter: (params: unknown) => {
        const item = (params as { data: (typeof data)[number] }).data;
        return `${labels[item.first] ?? item.first} / ${labels[item.second] ?? item.second}\n相关性：${Number(item.value[2]).toFixed(4)}\n有效日期：${item.count}`;
      },
    },
    xAxis: {
      type: "category",
      data: names,
      axisLabel: {
        color: palette.muted,
        rotate: 35,
        formatter: (name: string) => labels[name] ?? name,
      },
      splitArea: { show: true, areaStyle: { color: [palette.surface] } },
    },
    yAxis: {
      type: "category",
      data: names,
      axisLabel: {
        color: palette.muted,
        formatter: (name: string) => labels[name] ?? name,
      },
      splitArea: { show: true, areaStyle: { color: [palette.surface] } },
    },
    visualMap: {
      min: -1,
      max: 1,
      calculable: false,
      orient: "horizontal",
      left: "center",
      bottom: 0,
      text: ["正相关", "负相关"],
      textStyle: { color: palette.text },
      inRange: { color: [palette.primary, palette.surface, palette.series[3]] },
    },
    series: [
      {
        type: "heatmap",
        data,
        label: {
          show: names.length <= 12,
          color: palette.text,
          formatter: (params: unknown) =>
            Number((params as { value: number[] }).value[2]).toFixed(2),
        },
        emphasis: { itemStyle: { borderColor: palette.text, borderWidth: 1 } },
      },
    ],
  };
}

export function correlationHistoryOption(
  points: Array<{ date: string; rank_correlation: number | null }>,
  palette: ChartPalette,
): EChartsOption {
  const option = icChartOption(
    points.map((point) => ({ date: point.date, ic: point.rank_correlation })),
    "daily",
    palette,
  );
  return {
    ...option,
    legend: { show: false },
    series: [
      {
        name: "每日秩相关",
        type: "line",
        data: points.map((point) => point.rank_correlation),
        showSymbol: false,
        connectNulls: false,
        itemStyle: { color: palette.primary },
      },
    ],
  };
}

export function horizontalIntervalOption(
  mean: number,
  ciLow: number,
  ciHigh: number,
  palette: ChartPalette,
): EChartsOption {
  const span = Math.max(
    Math.abs(ciLow),
    Math.abs(ciHigh),
    Math.abs(mean),
    0.01,
  );
  const pad = Math.max(span * 0.2, 0.005);
  const min = Number(Math.min(0, ciLow - pad).toFixed(4));
  const max = Number(Math.max(0, ciHigh + pad).toFixed(4));
  return {
    backgroundColor: "transparent",
    textStyle: { color: palette.text, fontFamily: "inherit" },
    grid: { left: 82, right: 28, top: 24, bottom: 36 },
    tooltip: {
      trigger: "item",
      renderMode: "richText",
      backgroundColor: palette.surface,
      textStyle: { color: palette.text },
      formatter: () =>
        `均值：${mean.toFixed(6)}\n95% 区间：[${ciLow.toFixed(6)}, ${ciHigh.toFixed(6)}]`,
    },
    xAxis: {
      type: "value",
      min,
      max,
      axisLabel: {
        color: palette.muted,
        formatter: (value: number) => value.toFixed(3),
      },
      splitLine: { lineStyle: { color: palette.split } },
    },
    yAxis: {
      type: "category",
      data: ["Rank IC"],
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: palette.text },
    },
    series: [
      {
        name: "95% 区间",
        type: "line",
        data: [
          [ciLow, "Rank IC"],
          [ciHigh, "Rank IC"],
        ],
        showSymbol: true,
        symbol: "rect",
        symbolSize: [2, 12],
        lineStyle: { width: 3, color: palette.primary },
        itemStyle: { color: palette.primary },
        markLine: {
          silent: true,
          symbol: "none",
          data: [{ xAxis: 0 }],
          lineStyle: { color: palette.muted, type: "dashed", width: 1.5 },
          label: { show: false },
        },
      },
      {
        name: "均值",
        type: "scatter",
        data: [[mean, "Rank IC"]],
        symbolSize: 10,
        itemStyle: { color: palette.series[3] ?? palette.primary },
      },
    ],
  };
}
