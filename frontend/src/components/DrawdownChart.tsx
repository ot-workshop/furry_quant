import { useMemo } from "react";
import type { EChartsOption } from "echarts";
import type { EquityPoint } from "../types";
import { Chart } from "./Chart";

interface DrawdownChartProps {
  items: EquityPoint[];
}

export function DrawdownChart({ items }: DrawdownChartProps) {
  const option = useMemo<EChartsOption>(() => {
    const dates = items.map((item) => item.date);
    const drawdown = items.map((item) =>
      typeof item.drawdown === "number"
        ? Math.round(item.drawdown * 10000) / 100
        : null,
    );

    return {
      color: ["#b42318"],
      animationDuration: 320,
      grid: { left: 18, right: 24, top: 26, bottom: 24, containLabel: true },
      tooltip: {
        trigger: "axis",
        valueFormatter: (value) => {
          const numeric = Number(value);
          return Number.isFinite(numeric) ? `${numeric.toFixed(2)}%` : "—";
        },
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
        max: 0,
        name: "回撤",
        nameTextStyle: { color: "#8493a3", fontSize: 11, padding: [0, 0, 0, 8] },
        splitLine: { lineStyle: { color: "#eef2f6" } },
        axisLabel: { color: "#6b7b8c", fontSize: 11, formatter: "{value}%" },
      },
      series: [
        {
          name: "回撤",
          type: "line" as const,
          data: drawdown,
          showSymbol: false,
          connectNulls: false,
          smooth: false,
          lineStyle: { width: 1.8 },
          areaStyle: { color: "rgba(180, 35, 24, 0.12)" },
        },
      ],
    };
  }, [items]);

  return (
    <Chart
      option={option}
      height={230}
      ariaLabel="回测期间策略回撤走势图，纵轴单位为百分比"
    />
  );
}
