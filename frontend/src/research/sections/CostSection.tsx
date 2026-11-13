import {
  Collapse,
  Form,
  InputNumber,
  Segmented,
  Space,
  Typography,
} from "antd";
import { type FormState } from "../form";
import type { Update } from "./types";
export function CostSection({
  form,
  update,
}: {
  form: FormState;
  update: Update;
}) {
  const legacy = form.costMode === "legacy";
  const summary = legacy
    ? `综合费率 买入 ${form.openCost}% / 卖出 ${form.closeCost}%，冲击成本 ${form.impactCost}%`
    : `佣金 ${form.commissionRate}%，${form.taxMode === "historical" ? "历史税费" : "自定义税费"}，冲击成本 ${form.impactCost}%`;
  return (
    <Collapse
      items={[
        {
          key: "cost",
          label: (
            <Space>
              <Typography.Text strong>4. 交易成本</Typography.Text>
              <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                {summary}
              </Typography.Text>
            </Space>
          ),
          children: (
            <Form layout="vertical">
              <Form.Item label="费用模式">
                <Segmented
                  value={form.costMode}
                  onChange={(value) =>
                    update({ costMode: value as FormState["costMode"] })
                  }
                  options={[
                    { value: "legacy", label: "综合费率" },
                    {
                      value: "itemized",
                      label: "分项（佣金 + 印花税 + 过户费）",
                    },
                  ]}
                />
              </Form.Item>
              <div className="grid-3">
                {legacy ? (
                  <>
                    <Form.Item label="买入综合费率">
                      <Pct
                        value={form.openCost}
                        onChange={(openCost) => update({ openCost })}
                      />
                    </Form.Item>
                    <Form.Item label="卖出综合费率">
                      <Pct
                        value={form.closeCost}
                        onChange={(closeCost) => update({ closeCost })}
                      />
                    </Form.Item>
                  </>
                ) : (
                  <>
                    <Form.Item
                      label="券商佣金（双边）"
                      tooltip="含经手费等规费，不含印花税与过户费"
                    >
                      <Pct
                        value={form.commissionRate}
                        onChange={(commissionRate) =>
                          update({ commissionRate })
                        }
                      />
                    </Form.Item>
                    <Form.Item label="印花税与过户费">
                      <Segmented
                        block
                        value={form.taxMode}
                        onChange={(value) =>
                          update({ taxMode: value as FormState["taxMode"] })
                        }
                        options={[
                          { value: "historical", label: "按历史费率" },
                          { value: "fixed", label: "自定义" },
                        ]}
                      />
                    </Form.Item>
                    {form.taxMode === "fixed" ? (
                      <>
                        <Form.Item label="卖出印花税">
                          <Pct
                            value={form.stampTaxRate}
                            onChange={(stampTaxRate) =>
                              update({ stampTaxRate })
                            }
                          />
                        </Form.Item>
                        <Form.Item label="过户费（双边）">
                          <Pct
                            value={form.transferFeeRate}
                            onChange={(transferFeeRate) =>
                              update({ transferFeeRate })
                            }
                          />
                        </Form.Item>
                      </>
                    ) : null}
                  </>
                )}
                <Form.Item
                  label="冲击成本（双边）"
                  tooltip="按成交额比例扣除的滑点假设，默认 0.05%"
                >
                  <Pct
                    value={form.impactCost}
                    onChange={(impactCost) => update({ impactCost })}
                  />
                </Form.Item>
                <Form.Item label={legacy ? "单笔最低费用" : "单笔最低佣金"}>
                  <InputNumber
                    min={0}
                    value={form.minCost}
                    onChange={(v) => update({ minCost: Number(v ?? 0) })}
                    style={{ width: "100%" }}
                    suffix="元"
                  />
                </Form.Item>
              </div>
            </Form>
          ),
        },
      ]}
    />
  );
}

function Pct({
  value,
  onChange,
}: {
  value: number;
  onChange: (value: number) => void;
}) {
  return (
    <InputNumber
      min={0}
      max={99}
      step={0.01}
      value={value}
      onChange={(v) => onChange(Number(v ?? 0))}
      style={{ width: "100%" }}
      suffix="%"
    />
  );
}
