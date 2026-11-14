import { Flex, Select, Switch, Typography } from "antd";
import type { IndustryLevel, ResearchOptions } from "../../api/research";
import { defaultIndustryLevel, industryLevels, type FormState } from "../form";
import type { Update } from "./types";

export function NeutralizationControls({
  form,
  options,
  update,
}: {
  form: FormState;
  options: ResearchOptions;
  update: Update;
}) {
  return (
    <Flex align="center" gap={12} style={{ marginTop: 16 }}>
      <Switch
        checked={form.neutralization === "industry"}
        disabled={!options.industry_data}
        onChange={(checked) =>
          update({
            neutralization: checked ? "industry" : "off",
            industryVersion: options.industry_data?.id ?? "",
            industryLevel: checked
              ? industryLevels(options).includes(form.industryLevel)
                ? form.industryLevel
                : defaultIndustryLevel(options)
              : form.industryLevel,
          })
        }
      />
      <Typography.Text>行业中性化</Typography.Text>
      <Select<IndustryLevel>
        size="small"
        value={form.industryLevel}
        disabled={form.neutralization !== "industry"}
        onChange={(industryLevel) => update({ industryLevel })}
        options={industryLevels(options).map((level) => ({
          value: level,
          label: `${options.industry_data?.levels?.[level]?.label ?? level}行业`,
        }))}
        style={{ width: 110 }}
      />
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        {options.industry_data
          ? `按当日历史申万${form.industryLevel === "L1" ? "一级" : form.industryLevel === "L2" ? "二级" : "三级"}行业去除行业均值（行业数据覆盖 ${options.industry_data.instruments} 只，核验至 ${options.industry_data.data_asof ?? "未记录"}；历史公开时点未认证）`
          : "历史行业数据尚未准备，暂不可用"}
      </Typography.Text>
    </Flex>
  );
}
