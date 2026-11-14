import {
  Button,
  Card,
  Descriptions,
  Form,
  Input,
  Segmented,
  Select,
  Space,
  Typography,
} from "antd";
import type { ResearchOptions } from "../../api/research";
import {
  customStocks,
  dataRange,
  earliestStart,
  historicalOption,
  marketOption,
  selectedCoverage,
  type FormState,
} from "../form";
import type { Update } from "./types";

export function DataSection({
  form,
  options,
  update,
}: {
  form: FormState;
  options: ResearchOptions;
  update: Update;
}) {
  const historicalChoices = options.historical_indices ?? [];
  const marketChoices = options.market_universes ?? [];
  const stocks = customStocks(form, options);
  const coverage = selectedCoverage(form, options);
  const range = dataRange(form, options);
  const selected =
    form.sourceMode === "market_warehouse"
      ? marketOption(options, form.marketWarehouseId)
      : form.sourceMode === "index_warehouse"
        ? historicalOption(options, form.warehouseId)
        : null;

  function changeData(patch: Partial<FormState>) {
    const next = { ...form, ...patch };
    const nextRange = dataRange(next, options);
    const earliest = earliestStart(next, options);
    if (nextRange) {
      const lower = earliest ?? nextRange.start;
      patch.start =
        next.start < lower || next.start > nextRange.end ? lower : next.start;
      patch.end =
        next.end < patch.start || next.end > nextRange.end
          ? nextRange.end
          : next.end;
    }
    update(patch);
  }

  function chooseCustom(snapshotId: string) {
    const available = new Set(
      customStocks({ ...form, snapshotId }, options).map((item) => item.code),
    );
    const codes = form.codes.filter((code) => available.has(code));
    changeData({
      snapshotId,
      codes,
      ...(codes.length ? { topk: Math.min(form.topk, codes.length) } : {}),
    });
  }

  const countText = (value?: { covered: number; total: number }) =>
    value ? `${value.covered} / ${value.total} 只` : "未提供覆盖记录";
  return (
    <Card title="1. 股票池与数据">
      <Form layout="vertical">
        <Form.Item label="研究名称" required>
          <Input
            value={form.name}
            maxLength={80}
            showCount
            onChange={(event) => update({ name: event.target.value })}
          />
        </Form.Item>
        <Form.Item label="基础股票池">
          <Segmented
            value={form.sourceMode}
            onChange={(value) => {
              const sourceMode = value as FormState["sourceMode"];
              const patch: Partial<FormState> = { sourceMode };
              if (
                sourceMode === "snapshot" &&
                !form.snapshotId &&
                marketChoices.length
              ) {
                patch.snapshotId = marketChoices[0].id;
                patch.codes = [];
              }
              changeData(patch);
            }}
            options={[
              {
                value: "index_warehouse",
                label: "沪深300历史成分",
                disabled: !historicalChoices.length,
              },
              {
                value: "market_warehouse",
                label: "全市场",
                disabled: !marketChoices.length,
              },
              {
                value: "snapshot",
                label: "自选股票",
                disabled: !options.snapshots.length && !marketChoices.length,
              },
            ]}
          />
        </Form.Item>
        <Form.Item
          label={
            form.sourceMode === "index_warehouse"
              ? "历史成分版本"
              : "行情数据集"
          }
          extra="仅使用已下载的数据，不会因切换股票池或筛选条件重复下载行情。"
        >
          {form.sourceMode === "snapshot" ? (
            <Select
              value={form.snapshotId || undefined}
              onChange={chooseCustom}
              placeholder="选择包含目标股票的数据集"
              options={[
                {
                  label: "共享行情仓库",
                  options: marketChoices.map((item) => ({
                    value: item.id,
                    label: `${item.label} · ${item.codes_count}只 · ${item.start} ~ ${item.end}`,
                  })),
                },
                {
                  label: "行情快照",
                  options: options.snapshots.map((item) => ({
                    value: item.id,
                    label: item.label,
                  })),
                },
              ]}
            />
          ) : form.sourceMode === "market_warehouse" ? (
            <Select
              value={form.marketWarehouseId || undefined}
              onChange={(marketWarehouseId: string) =>
                changeData({ marketWarehouseId })
              }
              options={marketChoices.map((item) => ({
                value: item.id,
                label: `${item.label} · ${item.codes_count}只 · ${item.start} ~ ${item.end}`,
              }))}
            />
          ) : (
            <Select
              value={form.warehouseId || undefined}
              onChange={(warehouseId: string) => changeData({ warehouseId })}
              options={historicalChoices.map((item) => ({
                value: item.id,
                label: `${item.start} ~ ${item.end} · 历史涉及${item.codes_count}只`,
              }))}
            />
          )}
        </Form.Item>
        {form.sourceMode === "snapshot" && stocks.length ? (
          <Form.Item
            label={`自选股票（已选 ${form.codes.length} / ${stocks.length} 只）`}
            extra="从数据集中选择股票作为候选范围，再在筛选条件中设置每日准入规则。"
          >
            <Select
              mode="multiple"
              value={form.codes}
              onChange={(codes: string[]) => update({ codes })}
              options={stocks.map((item) => ({
                value: item.code,
                label: `${item.name === item.code ? "" : item.name + " "}${item.code}`,
              }))}
              optionFilterProp="label"
              maxTagCount="responsive"
              allowClear
              placeholder="搜索股票名称或代码"
            />
            <Space style={{ marginTop: 8 }}>
              <Button
                size="small"
                onClick={() =>
                  update({ codes: stocks.map((item) => item.code) })
                }
              >
                全选
              </Button>
              <Button size="small" onClick={() => update({ codes: [] })}>
                清空
              </Button>
            </Space>
          </Form.Item>
        ) : null}
        {coverage && range ? (
          <>
            <Typography.Title level={5}>实际数据覆盖</Typography.Title>
            <Descriptions
              size="small"
              column={{ xs: 1, md: 2 }}
              items={[
                {
                  key: "scope",
                  label: "覆盖范围",
                  children:
                    form.sourceMode === "snapshot"
                      ? `自选 ${form.codes.length} 只，数据集共 ${stocks.length} 只`
                      : form.sourceMode === "index_warehouse"
                        ? `沪深300历史成分，历年涉及 ${selected?.codes_count} 只`
                        : selected?.full_market
                          ? `沪深A股全市场，历史涉及 ${selected.codes_count} 只`
                          : `已下载子集 ${selected?.codes_count ?? 0} 只，未覆盖完整全市场`,
                },
                {
                  key: "dates",
                  label: "研究日期范围",
                  children: `${range.start} ~ ${range.end}`,
                },
                {
                  key: "warmup",
                  label: "行情及预热起点",
                  children: range.dataStart,
                },
                {
                  key: "names",
                  label: "历史名称记录",
                  children: countText(coverage.st),
                },
                {
                  key: "listing",
                  label: "历史上市日期",
                  children: countText(coverage.listing),
                },
                {
                  key: "financial",
                  label: "财务记录（数据集）",
                  children: countText(
                    coverage.option.financial_coverage ?? undefined,
                  ),
                },
              ]}
            />
            <Typography.Paragraph type="secondary" style={{ marginTop: 12 }}>
              覆盖数量表示数据集中有记录的股票数，不代表每个历史日期均有可用状态或满足因子要求。
              {form.sourceMode === "index_warehouse"
                ? "成分按历史月度快照下一交易日生效，精确公告时点未认证。"
                : ""}
            </Typography.Paragraph>
          </>
        ) : null}
      </Form>
    </Card>
  );
}
