import { useMemo } from "react";
import type { EChartsOption } from "echarts";
import type { AnnualReturn } from "../types";
import { formatPercent } from "../utils/format";
import { Chart } from "./Chart";

export function annualReturnsOption(items: AnnualReturn[]): EChartsOption {
  return {
    animationDuration: 240,
    grid: { left: 12, right: 18, top: 44, bottom: 30, containLabel: true },
    legend: { top: 0, textStyle: { color: "#526477", fontSize: 11 } },
    tooltip: { trigger: "axis", valueFormatter: value => typeof value === "number" ? formatPercent(value) : "—" },
    xAxis: { type: "category", data: items.map(row => String(row.year)), axisTick: { show: false }, axisLine: { lineStyle: { color: "#d8e1ea" } } },
    yAxis: { type: "value", axisLabel: { formatter: value => formatPercent(value, 0) }, splitLine: { lineStyle: { color: "#eef2f6" } } },
    series: [
      { name: "策略收益", type: "bar", barMaxWidth: 48,
        data: items.map(row => ({ value: row.strategy_return, itemStyle: { color: (row.strategy_return ?? 0) >= 0 ? "#27ae60" : "#e74c3c" } })),
        label: { show: true, position: "top", fontSize: 11, formatter: params => typeof params.value === "number" ? formatPercent(params.value, 1) : "—" } },
      { name: "基准收益", type: "bar", barMaxWidth: 48, itemStyle: { color: "#a8b5c5" }, data: items.map(row => row.benchmark_return) },
    ],
  };
}

export function AnnualReturnsChart({ items }: { items: AnnualReturn[] }) {
  const option = useMemo(() => annualReturnsOption(items), [items]);
  return <Chart option={option} height={350} ariaLabel="各年度回测覆盖区间的策略与基准收益柱状图" />;
}
