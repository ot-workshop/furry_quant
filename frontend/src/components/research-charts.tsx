import { Grid } from "antd";
import * as echarts from "echarts/core";
import { HeatmapChart, ScatterChart } from "echarts/charts";
import { AriaComponent, VisualMapComponent } from "echarts/components";
import { Chart } from "./charts";
import {
  correlationChartOption,
  correlationHistoryOption,
  horizontalIntervalOption,
  icChartOption,
  type ICChartMetric,
} from "../research/chart-options";
import type { CorrelationCell, RollingICPoint } from "../types";

echarts.use([HeatmapChart, ScatterChart, VisualMapComponent, AriaComponent]);

export function ICSeriesChart({
  points,
  metric,
  group,
}: {
  points: RollingICPoint[];
  metric: ICChartMetric;
  group: string;
}) {
  return (
    <Chart
      height={290}
      ariaLabel={`${metric} IC 与 Rank IC 时间序列`}
      group={group}
      build={(palette) => icChartOption(points, metric, palette)}
      deps={[points, metric]}
    />
  );
}

export function CorrelationHeatmap({
  names,
  cells,
  labels,
  onSelect,
}: {
  names: string[];
  cells: CorrelationCell[];
  labels: Record<string, string>;
  onSelect: (first: string, second: string) => void;
}) {
  return (
    <Chart
      height={Math.max(390, names.length * 32 + 150)}
      ariaLabel="因子相关性热图"
      build={(palette, width) =>
        correlationChartOption(names, cells, labels, palette, width)
      }
      deps={[names, cells, labels]}
      onDataClick={(data) => {
        const cell = data as {
          first?: string;
          second?: string;
          noSample?: boolean;
        };
        if (cell?.first && cell?.second && !cell.noSample) {
          onSelect(cell.first, cell.second);
        }
      }}
    />
  );
}

export function CorrelationHistoryChart({
  points,
}: {
  points: Array<{ date: string; rank_correlation: number | null }>;
}) {
  return (
    <Chart
      height={270}
      ariaLabel="因子对每日秩相关"
      build={(palette) => correlationHistoryOption(points, palette)}
      deps={[points]}
    />
  );
}

export function HorizontalIntervalChart({
  mean,
  ciLow,
  ciHigh,
  ariaLabel,
}: {
  mean: number;
  ciLow: number;
  ciHigh: number;
  ariaLabel: string;
}) {
  const screens = Grid.useBreakpoint();
  const compact =
    screens.sm === false ||
    (typeof window !== "undefined" && window.innerWidth < 576);
  return (
    <Chart
      height={120}
      ariaLabel={ariaLabel}
      build={(palette) =>
        horizontalIntervalOption(mean, ciLow, ciHigh, palette, compact)
      }
      deps={[mean, ciLow, ciHigh, compact]}
    />
  );
}
