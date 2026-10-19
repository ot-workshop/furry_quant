import { useMemo } from "react";
import type { EChartsOption } from "echarts";
import type { EquityPoint } from "../types";
import { formatNumber } from "../utils/format";
import { Chart } from "./Chart";

interface EquityChartProps {
  items: EquityPoint[];
}

export function EquityChart({ items }: EquityChartProps) {
  const option = useMemo<EChartsOption>(() => {
    const dates = items.map((item) => item.date);
    const equity = items.map((item) =>
      typeof item.equity === "number" ? item.equity : null,
    );
    const benchmark = items.map((item) =>
      typeof item.benchmark === "number" ? item.benchmark : null,
    );

    return {
      color: ["#0f766e", "#7c8ea3"],
      animationDuration: 320,
      grid: { left: 18, right: 24, top: 46, bottom: 62, containLabel: true },
      tooltip: {
        trigger: "axis",
        valueFormatter: (value) => {
          const numeric = Number(value);
          return Number.isFinite(numeric) ? formatNumber(numeric, 4) : "—";
        },
      },
      legend: {
        top: 0,
        right: 0,
        itemWidth: 16,
        itemHeight: 8,
        textStyle: { color: "#526477", fontSize: 12 },
      },
      dataZoom: [
        { type: "inside" as const },
        {
          type: "slider" as const,
          height: 16,
          bottom: 8,
          borderColor: "transparent",
          backgroundColor: "#f1f5f9",
          fillerColor: "rgba(15, 118, 110, 0.12)",
          handleStyle: { color: "#0f766e" },
          textStyle: { color: "#7a8b9c", fontSize: 10 },
        },
      ],
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
        scale: true,
        name: "净值",
        nameTextStyle: { color: "#8493a3", fontSize: 11, padding: [0, 0, 0, 8] },
        splitLine: { lineStyle: { color: "#eef2f6" } },
        axisLabel: { color: "#6b7b8c", fontSize: 11 },
      },
      series: [
        {
          name: "策略净值",
          type: "line" as const,
          data: equity,
          showSymbol: false,
          symbol: "circle",
          symbolSize: 5,
          connectNulls: false,
          smooth: false,
          lineStyle: { width: 2.4 },
          areaStyle: { color: "rgba(15, 118, 110, 0.08)" },
        },
        {
          name: "基准净值",
          type: "line" as const,
          data: benchmark,
          showSymbol: false,
          symbol: "circle",
          symbolSize: 5,
          connectNulls: false,
          smooth: false,
          lineStyle: { width: 1.8, type: "dashed" as const },
        },
      ],
    };
  }, [items]);

  return (
    <Chart
      option={option}
      height={360}
      ariaLabel="回测策略净值与基准净值走势图，可拖动下方缩放条查看区间"
    />
  );
}
