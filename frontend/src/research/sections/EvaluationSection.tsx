import { Collapse, Form, InputNumber, Select, Space, Typography } from "antd";
import { type FormState } from "../form";
import type { Update } from "./types";
export function EvaluationSection({
  form,
  update,
}: {
  form: FormState;
  update: Update;
}) {
  return (
    <Collapse
      items={[
        {
          key: "evaluation",
          label: (
            <Space>
              <Typography.Text strong>5. 因子评估</Typography.Text>
              <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                持有期 {form.horizons.join("、")} 日 · {form.groups} 组
              </Typography.Text>
            </Space>
          ),
          children: (
            <Form layout="vertical">
              <div className="grid-3">
                <Form.Item
                  label="评估持有期（交易日）"
                  tooltip="只用于计算 IC 与分组收益，不影响回测"
                >
                  <Select
                    mode="tags"
                    value={form.horizons.map(String)}
                    onChange={(values: string[]) =>
                      update({
                        horizons: values
                          .map(Number)
                          .filter((n) => Number.isInteger(n) && n > 0),
                      })
                    }
                    options={[1, 5, 10, 20].map((n) => ({
                      value: String(n),
                      label: `${n} 日`,
                    }))}
                    tokenSeparators={[",", " ", "，"]}
                  />
                </Form.Item>
                <Form.Item label="分组数">
                  <InputNumber
                    min={2}
                    max={10}
                    value={form.groups}
                    onChange={(v) => update({ groups: Number(v ?? 2) })}
                    style={{ width: "100%" }}
                  />
                </Form.Item>
                <Form.Item label="计算 IC 的最少股票数">
                  <InputNumber
                    min={3}
                    value={form.minCrossSection}
                    onChange={(v) =>
                      update({ minCrossSection: Number(v ?? 3) })
                    }
                    style={{ width: "100%" }}
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
