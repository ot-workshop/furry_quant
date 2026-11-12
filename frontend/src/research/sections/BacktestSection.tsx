import { useMemo } from "react";
import {
  Button,
  Card,
  DatePicker,
  Form,
  InputNumber,
  Segmented,
  Select,
  Space,
} from "antd";
import dayjs, { type Dayjs } from "dayjs";
import type { ResearchOptions } from "../../api/research";
import { dataRange, tradingDates, type FormState } from "../form";
import type { Update } from "./types";
export function BacktestSection({
  form,
  options,
  update,
  earliest,
}: {
  form: FormState;
  options: ResearchOptions;
  update: Update;
  earliest: string | null;
}) {
  const dates = useMemo(
    () => new Set(tradingDates(form, options)),
    [form, options],
  );
  const range = dataRange(form, options);
  const disabledDate = (day: Dayjs) => {
    const text = day.format("YYYY-MM-DD");
    if (range && (text < range.start || text > range.end)) return true;
    return dates.size > 0 && !dates.has(text);
  };
  const benchmarks = options.comparison_benchmarks ?? options.benchmarks;
  return (
    <Card title="3. 回测设置">
      <Form layout="vertical">
        <Form.Item
          label="回测区间"
          extra={
            earliest ? (
              <Space size={4}>
                <span>
                  按当前因子窗口，最早可从 {earliest}{" "}
                  开始（之前的数据用于预热）。
                </span>
                {form.start !== earliest ? (
                  <Button
                    size="small"
                    type="link"
                    onClick={() => update({ start: earliest })}
                  >
                    使用该日期
                  </Button>
                ) : null}
              </Space>
            ) : (
              "只能选择数据集内的交易日。"
            )
          }
        >
          <DatePicker.RangePicker
            value={[
              form.start ? dayjs(form.start) : null,
              form.end ? dayjs(form.end) : null,
            ]}
            disabledDate={disabledDate}
            allowClear={false}
            onChange={(values) =>
              update({
                start: values?.[0]?.format("YYYY-MM-DD") ?? "",
                end: values?.[1]?.format("YYYY-MM-DD") ?? "",
              })
            }
          />
        </Form.Item>
        <div className="grid-3">
          <Form.Item label="目标持股数量">
            <InputNumber
              min={1}
              max={300}
              value={form.topk}
              onChange={(v) => update({ topk: Number(v ?? 1) })}
              style={{ width: "100%" }}
              suffix="只"
            />
          </Form.Item>
          <Form.Item label="初始资金">
            <InputNumber
              min={1}
              step={10000}
              value={form.initialCash}
              onChange={(v) => update({ initialCash: Number(v ?? 0) })}
              style={{ width: "100%" }}
              suffix="元"
            />
          </Form.Item>
          <Form.Item
            label="评价基准"
            tooltip="只影响基准与相对绩效，不影响选股与成交"
          >
            <Select
              value={form.benchmark}
              onChange={(benchmark: string) => update({ benchmark })}
              options={[
                { value: "", label: "沿用数据集基准" },
                ...benchmarks.map((item) => ({
                  value: item.code,
                  label: `${item.name}（${item.code}）`,
                })),
              ]}
            />
          </Form.Item>
        </div>
        <div className="grid-3">
          <Form.Item
            label="调仓频率"
            tooltip="调仓日使用前一交易日收盘后的评分，次日开盘成交"
          >
            <Segmented
              block
              value={form.rebalanceFrequency}
              onChange={(value) =>
                update({
                  rebalanceFrequency: value as FormState["rebalanceFrequency"],
                })
              }
              options={[
                { value: "daily", label: "每日" },
                { value: "weekly", label: "每周" },
                { value: "monthly", label: "每月" },
              ]}
            />
          </Form.Item>
          <Form.Item
            label="每次最多替换"
            tooltip="每个调仓日最多卖出并替换的持仓数量"
          >
            <InputNumber
              min={0}
              max={form.topk}
              value={form.nDrop}
              onChange={(v) => update({ nDrop: Number(v ?? 0) })}
              style={{ width: "100%" }}
              suffix="只"
            />
          </Form.Item>
          <Form.Item
            label="股票仓位"
            tooltip="可用资金中投入股票的比例，其余保留现金"
          >
            <InputNumber
              min={1}
              max={100}
              value={form.riskDegree}
              onChange={(v) => update({ riskDegree: Number(v ?? 0) })}
              style={{ width: "100%" }}
              suffix="%"
            />
          </Form.Item>
        </div>
      </Form>
    </Card>
  );
}
