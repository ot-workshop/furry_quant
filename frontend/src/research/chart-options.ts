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

export function correlationMatrixStats(
  names: string[],
  cells: CorrelationCell[],
) {
  const validFactors = new Set<string>();
  let validCellCount = 0;
  for (const cell of cells) {
    if (cell.value !== null && Number.isFinite(cell.value)) {
      validCellCount += 1;
      validFactors.add(cell.first);
      validFactors.add(cell.second);
    }
  }
  const uncomputableFactors = names.filter((name) => !validFactors.has(name));
  return {
    uncomputableFactors,
    validCellCount,
    allUncomputable: names.length > 0 && validCellCount === 0,
  };
}

export function correlationChartOption(
  names: string[],
  cells: CorrelationCell[],
  labels: Record<string, string>,
  palette: ChartPalette,
  width = 720,
): EChartsOption {
  const effectiveWidth = width > 0 ? width : 720;
  const compact = effectiveWidth < 520;
  const left = compact ? 88 : 136;
  const right = compact ? 14 : 28;
  const top = compact ? 16 : 24;
  const bottom = compact ? 96 : 112;
  const cellPx = (effectiveWidth - left - right) / Math.max(1, names.length);
  const isDark = /^#(0|1|2)/i.test(palette.surface.trim());
  const noSampleFill = isDark ? "#262626" : "#eaedf1";
  const noSampleBorder = isDark ? "#595959" : "#bfbfbf";
  const noSampleText = isDark ? "#a6a6a6" : "#595959";
  const decalColor = isDark
    ? "rgba(255, 255, 255, 0.22)"
    : "rgba(0, 0, 0, 0.22)";
  const decal =
    cellPx >= 56
      ? {
          symbol: "rect",
          dashArrayX: [1, 0],
          dashArrayY: [3, 5],
          rotation: Math.PI / 4,
          color: decalColor,
        }
      : {
          symbol: "circle",
          symbolSize: 0.8,
          dashArrayX: [
            [2, 4],
            [4, 2],
          ],
          dashArrayY: [2, 4],
          color: decalColor,
        };

  const cellMap = new Map<string, CorrelationCell>();
  for (const cell of cells) {
    cellMap.set(`${cell.first}\0${cell.second}`, cell);
    cellMap.set(`${cell.second}\0${cell.first}`, cell);
  }

  type HeatmapItem = {
    value: [number, number, number];
    first: string;
    second: string;
    count: number;
    noSample: boolean;
  };
  const validData: HeatmapItem[] = [];
  const missingData: HeatmapItem[] = [];

  for (let y = 0; y < names.length; y += 1) {
    for (let x = 0; x < names.length; x += 1) {
      const first = names[x];
      const second = names[y];
      const cell = cellMap.get(`${first}\0${second}`);
      if (cell && cell.value !== null && Number.isFinite(cell.value)) {
        validData.push({
          value: [x, y, cell.value],
          first,
          second,
          count: cell.valid_dates,
          noSample: false,
        });
      } else {
        missingData.push({
          value: [x, y, 0],
          first,
          second,
          count: cell?.valid_dates ?? 0,
          noSample: true,
        });
      }
    }
  }

  return {
    backgroundColor: "transparent",
    textStyle: { color: palette.text, fontFamily: "inherit" },
    grid: { left, right, top, bottom },
    tooltip: {
      trigger: "item",
      confine: true,
      renderMode: "richText",
      backgroundColor: palette.surface,
      borderColor: palette.border,
      textStyle: { color: palette.text },
      formatter: (params: unknown) => {
        const item = (params as { data: HeatmapItem }).data;
        const pairTitle = `${labels[item.first] ?? item.first} / ${labels[item.second] ?? item.second}`;
        if (item.noSample) {
          return `${pairTitle}\n相关性：无法计算相关系数（有效日期不足，或因子在区间内没有变化）\n有效日期：${item.count}`;
        }
        return `${pairTitle}\n相关性：${Number(item.value[2]).toFixed(4)}\n有效日期：${item.count}`;
      },
    },
    xAxis: {
      type: "category",
      data: names,
      axisLine: { show: true, lineStyle: { color: palette.border } },
      axisTick: { show: true, alignWithLabel: true },
      axisLabel: {
        color: palette.muted,
        rotate: compact ? 40 : 35,
        fontSize: compact ? 11 : 12,
        width: compact ? 76 : 110,
        overflow: "truncate",
        formatter: (name: string) => labels[name] ?? name,
      },
      splitLine: { show: true, lineStyle: { color: palette.split, width: 1 } },
      splitArea: { show: false },
    },
    yAxis: {
      type: "category",
      data: names,
      inverse: true,
      axisLine: { show: true, lineStyle: { color: palette.border } },
      axisTick: { show: true, alignWithLabel: true },
      axisLabel: {
        color: palette.muted,
        fontSize: compact ? 11 : 12,
        width: left - 14,
        overflow: "truncate",
        formatter: (name: string) => labels[name] ?? name,
      },
      splitLine: { show: true, lineStyle: { color: palette.split, width: 1 } },
      splitArea: { show: false },
    },
    visualMap: {
      seriesIndex: 0,
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
        name: "相关性",
        type: "heatmap",
        data: validData,
        label: {
          show: names.length <= 12,
          color: palette.text,
          fontSize: compact ? 11 : 12,
          formatter: (params: unknown) =>
            Number((params as { value: number[] }).value[2]).toFixed(2),
        },
        emphasis: { itemStyle: { borderColor: palette.text, borderWidth: 1 } },
      },
      {
        name: "无法计算相关性",
        type: "heatmap",
        data: missingData,
        itemStyle: {
          color: noSampleFill,
          borderColor: noSampleBorder,
          borderWidth: 1,
          borderType: "dashed",
          decal,
        },
        label: {
          show: names.length <= 12,
          color: noSampleText,
          fontSize: compact ? 11 : 12,
          formatter: () => "—",
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
  compact = false,
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
  const compactInterval =
    compact && max > min ? Number(((max - min) / 2).toFixed(4)) : undefined;
  return {
    backgroundColor: "transparent",
    textStyle: { color: palette.text, fontFamily: "inherit" },
    grid: compact
      ? { left: 56, right: 16, top: 20, bottom: 34 }
      : { left: 82, right: 28, top: 24, bottom: 36 },
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
      ...(compactInterval !== undefined
        ? { interval: compactInterval }
        : compact
          ? { splitNumber: 2 }
          : {}),
      axisLabel: {
        color: palette.muted,
        hideOverlap: true,
        ...(compact
          ? {
              showMinLabel: true,
              showMaxLabel: true,
              alignMinLabel: "left",
              alignMaxLabel: "right",
              fontSize: 11,
            }
          : {}),
        formatter: (value: number) => value.toFixed(compact ? 2 : 3),
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
