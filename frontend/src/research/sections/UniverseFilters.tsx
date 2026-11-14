import {
  Card,
  Checkbox,
  Form,
  InputNumber,
  Select,
  Switch,
  Typography,
} from "antd";
import type {
  ResearchOptions,
  UniverseFilters as Filters,
} from "../../api/research";
import type { FormState } from "../form";
import type { Update } from "./types";

export function UniverseFilters({
  form,
  options,
  update,
}: {
  form: FormState;
  options: ResearchOptions;
  update: Update;
}) {
  const filters = form.universeFilters;
  const change = (patch: Partial<Filters>) =>
    update({ universeFilters: { ...filters, ...patch } });
  const amount = (
    field: "min_market_cap" | "max_market_cap" | "min_avg_amount",
    unit: number,
  ) => (
    <InputNumber
      min={0}
      value={filters[field] === null ? null : filters[field]! / unit}
      onChange={(value) =>
        change({ [field]: value === null ? null : Number(value) * unit })
      }
      style={{ width: "100%" }}
      placeholder="不限"
    />
  );
  const sectors =
    options.industry_data?.sectors?.filter(
      (item) => item.level === filters.industry_level,
    ) ?? [];
  return (
    <Card
      title="筛选条件"
      extra={
        <Switch
          checked={form.filtersEnabled}
          checkedChildren="开启"
          unCheckedChildren="关闭"
          aria-label="启用条件筛选"
          onChange={(filtersEnabled) => update({ filtersEnabled })}
        />
      }
    >
      <Typography.Paragraph type="secondary">
        在所选基础股票池内逐日筛选，沪深300、全市场和自选股票使用相同规则。
      </Typography.Paragraph>
      {form.filtersEnabled ? (
        <Form layout="vertical">
          <Form.Item label="交易板块">
            <Select
              mode="multiple"
              value={filters.boards}
              onChange={(boards) => change({ boards })}
              options={[
                { value: "main", label: "沪深主板" },
                { value: "chinext", label: "创业板" },
                { value: "star", label: "科创板" },
              ]}
            />
          </Form.Item>
          <div className="grid-3">
            <Form.Item label="上市至少（自然日）">
              <InputNumber
                min={0}
                max={36500}
                value={filters.min_listing_days}
                onChange={(value) =>
                  change({ min_listing_days: Number(value ?? 0) })
                }
                style={{ width: "100%" }}
              />
            </Form.Item>
            <Form.Item label="总市值下限（亿元）">
              {amount("min_market_cap", 100000000)}
            </Form.Item>
            <Form.Item label="总市值上限（亿元）">
              {amount("max_market_cap", 100000000)}
            </Form.Item>
            <Form.Item label="日均成交额下限（万元）">
              {amount("min_avg_amount", 10000)}
            </Form.Item>
            <Form.Item label="成交额窗口（交易日）">
              <InputNumber
                min={2}
                max={252}
                value={filters.amount_window}
                onChange={(value) =>
                  change({ amount_window: Number(value ?? 20) })
                }
                style={{ width: "100%" }}
              />
            </Form.Item>
            <Form.Item label="行业筛选级别">
              <Select
                value={filters.industry_level}
                onChange={(industry_level) =>
                  change({
                    industry_level,
                    industries: [],
                    industry_version: null,
                  })
                }
                options={[
                  { value: "L1", label: "申万一级" },
                  { value: "L2", label: "申万二级" },
                  { value: "L3", label: "申万三级" },
                ]}
              />
            </Form.Item>
          </div>
          <Form.Item
            label="纳入行业"
            extra="留空表示不限行业；按历史行业归属筛选，与因子行业中性化分别配置。"
          >
            <Select
              mode="multiple"
              allowClear
              value={filters.industries}
              disabled={!sectors.length}
              placeholder="不限行业"
              optionFilterProp="label"
              options={sectors.map((item) => ({
                value: item.industry_code,
                label: `${item.industry_name} ${item.industry_code}`,
              }))}
              onChange={(industries) =>
                change({
                  industries,
                  industry_version: options.industry_data?.id,
                })
              }
            />
          </Form.Item>
          <Form.Item tooltip="ST状态由历史名称变更识别，记录缺失或冲突时按未知处理；不会使用当前股票名称回填历史。">
            <Checkbox
              checked={filters.exclude_st}
              onChange={(event) => change({ exclude_st: event.target.checked })}
            >
              排除 ST 及状态未知股票
            </Checkbox>
          </Form.Item>
          <Typography.Paragraph type="secondary">
            每个信号日重新筛选，使用当日及以前的数据，下一交易日开盘执行。成交额窗口需要完整历史；股票被筛出后尝试退出，受交易限制时保留持仓并重试。
          </Typography.Paragraph>
        </Form>
      ) : (
        <Typography.Text type="secondary">
          未启用条件筛选，按基础股票池生成候选名单。
        </Typography.Text>
      )}
    </Card>
  );
}
