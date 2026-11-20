import { DatePicker } from "antd";
import dayjs from "dayjs";

export type AnalysisRange = [string, string] | null;

export function AnalysisDates({
  value,
  onChange,
  compact = false,
}: {
  value: AnalysisRange;
  onChange: (value: AnalysisRange) => void;
  compact?: boolean;
}) {
  return (
    <DatePicker.RangePicker
      aria-label="分析信号日范围"
      value={value ? [dayjs(value[0]), dayjs(value[1])] : null}
      placeholder={
        compact ? ["开始日", "结束日"] : ["信号开始日", "信号结束日"]
      }
      onChange={(days) =>
        onChange(
          days?.[0] && days?.[1]
            ? [days[0].format("YYYY-MM-DD"), days[1].format("YYYY-MM-DD")]
            : null,
        )
      }
    />
  );
}
