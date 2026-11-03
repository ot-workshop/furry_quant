import { useMemo } from "react";
import type { EChartsOption } from "echarts";
import type { FactorHistoryItem } from "../types";
import type { FactorDescriptor } from '../types';
import { factorLabel, formatNumber } from "../utils/format";
import { Chart } from "./Chart";

const SERIES_COLORS = [
  "#0f766e",
  "#2563eb",
  "#b45309",
  "#7c3aed",
  "#be123c",
  "#0891b2",
];

interface FactorHistoryChartProps {
  items: FactorHistoryItem[];
  scheme: string;
  scoreUnit?: string;
  definitions?: Record<string, FactorDescriptor>;
}

export function FactorHistoryChart({
  items,
  scheme,
  scoreUnit,
  definitions,
}: FactorHistoryChartProps) {
  const option = useMemo<EChartsOption>(() => {
    const keys = new Set<string>();
    for (const item of items) {
      for (const [key, value] of Object.entries(item)) {
        if (key === "date" || key === "run_id") continue;
        if (typeof value === "number") keys.add(key);
      }
    }
    const orderedKeys = Array.from(keys).sort((left, right) => {
      if (left === "composite") return 1;
      if (right === "composite") return -1;
      return left.localeCompare(right);
    });
    const isPercentile = scoreUnit === "percentile_0_100";
    const dates = items.map((item) => item.date);

    return {
      color: SERIES_COLORS,
      animationDuration: 320,
      grid: { left: 16, right: 24, top: 44, bottom: 20, containLabel: true },
      tooltip: {
        trigger: "axis",
        valueFormatter: (value) => {
          const numeric = Number(value);
          return Number.isFinite(numeric) ? formatNumber(numeric, 2) : "—";
        },
      },
      legend: {
        top: 0,
        right: 0,
        itemWidth: 14,
        itemHeight: 8,
        textStyle: { color: "#526477", fontSize: 12 },
      },
      xAxis: {
        type: "category",
        data: dates,
        boundaryGap: false,
        axisLine: { lineStyle: { color: "#d8e1ea" } },
        axisTick: { show: false },
        axisLabel: { color: "#6b7b8c", fontSize: 11 },
      },
      yAxis: {
        type: "value",
        min: isPercentile ? 0 : undefined,
        max: isPercentile ? 100 : undefined,
        splitLine: { lineStyle: { color: "#eef2f6" } },
        axisLabel: { color: "#6b7b8c", fontSize: 11 },
      },
      series: orderedKeys.map((key) => ({
        name: definitions?.[key] ? `${definitions[key].label}${definitions[key].name !== key ? ` [${key}]` : ''}` : factorLabel(key),
        type: "line" as const,
        smooth: false,
        symbol: "circle",
        symbolSize: 6,
        showSymbol: false,
        connectNulls: false,
        emphasis: { focus: "series" as const },
        lineStyle: { width: key === "composite" ? 3 : 2 },
        data: items.map((item) => {
          const value = item[key];
          return typeof value === "number" ? value : null;
        }),
      })),
    };
  }, [items, scoreUnit, definitions]);

  return (
    <Chart
      option={option}
      height={330}
      ariaLabel={`${scheme} 评分方案下的因子历史评分走势图`}
    />
  );
}
