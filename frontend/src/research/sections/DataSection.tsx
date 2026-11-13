import {
  Alert,
  Button,
  Card,
  Form,
  Input,
  Segmented,
  Select,
  Space,
  Typography,
} from "antd";
import type { ResearchOptions } from "../../api/research";
import { historicalOption, snapshotOf, type FormState } from "../form";
import type { Update } from "./types";
const SOURCE_LABELS: Record<string, string> = {
  official_tushare_shared: "官方共享行情",
  tushare_baostock: "联合数据",
};

export function DataSection({
  form,
  options,
  update,
}: {
  form: FormState;
  options: ResearchOptions;
  update: Update;
}) {
  const snapshot = snapshotOf(options, form.snapshotId);
  const historical = historicalOption(options, form.warehouseId);
  const historicalChoices = options.historical_indices ?? [];
  const exports = options.snapshots.filter((item) => !item.is_subset);
  const subsets = options.snapshots.filter((item) => item.is_subset);

  function chooseSnapshot(id: string) {
    const next = snapshotOf(options, id);
    if (!next) return;
    update({
      snapshotId: id,
      codes: next.codes.map((item) => item.code),
      topk: Math.min(form.topk, next.codes.length),
    });
  }
  return (
    <Card title="1. 数据与股票池">
      <Form layout="vertical">
        <Form.Item label="研究名称" required>
          <Input
            value={form.name}
            maxLength={80}
            showCount
            onChange={(e) => update({ name: e.target.value })}
          />
        </Form.Item>
        <Form.Item label="数据类型">
          <Segmented
            value={form.sourceMode}
            onChange={(value) =>
              update({ sourceMode: value as FormState["sourceMode"] })
            }
            options={[
              {
                value: "snapshot",
                label: "固定股票池",
                disabled: !options.snapshots.length,
              },
              {
                value: "index_warehouse",
                label: "沪深300历史成分（动态）",
                disabled: !historicalChoices.length,
              },
            ]}
          />
        </Form.Item>
        {form.sourceMode === "snapshot" ? (
          <>
            <Form.Item
              label="数据集"
              extra="数据集来自数据管理中的行情下载；需要其他股票时，请先下载包含它们的行情。"
            >
              <Select
                value={form.snapshotId || undefined}
                placeholder="选择数据集"
                onChange={chooseSnapshot}
                options={[
                  {
                    label: "行情下载",
                    options: exports.map((item) => ({
                      value: item.id,
                      label: `${SOURCE_LABELS[item.source] ?? "行情"} · ${item.codes.length} 只 · ${item.start} ~ ${item.end}${item.financial_coverage ? ` · 财报 ${item.financial_coverage.covered}/${item.financial_coverage.total}` : " · 无财报"}`,
                    })),
                  },
                  ...(subsets.length
                    ? [
                        {
                          label: "以往研究的股票子集",
                          options: subsets.map((item) => ({
                            value: item.id,
                            label: `${item.codes.length} 只 · ${item.start} ~ ${item.end}${item.financial_coverage ? ` · 财报 ${item.financial_coverage.covered}/${item.financial_coverage.total}` : " · 无财报"}`,
                          })),
                        },
                      ]
                    : []),
                ]}
              />
            </Form.Item>
            {snapshot ? (
              <Form.Item
                label={
                  <Space>
                    股票池
                    <Typography.Text type="secondary">
                      已选 {form.codes.length} / {snapshot.codes.length}
                    </Typography.Text>
                  </Space>
                }
                extra="至少 2 只；可从数据集中挑选任意子集。"
              >
                <Select
                  mode="multiple"
                  value={form.codes}
                  onChange={(codes: string[]) => update({ codes })}
                  options={snapshot.codes.map((item) => ({
                    value: item.code,
                    label: `${item.name} ${item.code}`,
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
                      update({ codes: snapshot.codes.map((item) => item.code) })
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
          </>
        ) : (
          <>
            <Form.Item label="历史成分版本">
              <Select
                value={form.warehouseId || undefined}
                onChange={(warehouseId: string) => update({ warehouseId })}
                options={historicalChoices.map((item) => ({
                  value: item.id,
                  label: `${item.start} ~ ${item.end}${item.shared_version ? ` · 共享版本 ${item.shared_version.slice(0, 8)}` : ""}${item.financial_coverage ? ` · 财报覆盖 ${item.financial_coverage.covered}/${item.financial_coverage.total}` : " · 无财报"}`,
                }))}
              />
            </Form.Item>
            {historical ? (
              <Alert
                type="info"
                showIcon
                title={`每日按当时的沪深300成分排名（约 ${historical.codes_count} 只股票参与过），含科创板`}
                description={`研究区间 ${historical.start} ~ ${historical.end}；预热数据自 ${historical.data_start} 起。调出成分的持仓会持续尝试卖出，停牌或跌停时保留并记录原因。${historical.note ?? ""}`}
              />
            ) : null}
          </>
        )}
      </Form>
    </Card>
  );
}
