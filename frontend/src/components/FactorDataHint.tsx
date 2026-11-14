import { useEffect, useState } from "react";
import { Alert, Button, Flex, Popover, Spin, Typography } from "antd";
import {
  ExclamationCircleOutlined,
  InfoCircleOutlined,
} from "@ant-design/icons";
import type { FactorDefinition, ResearchOptions } from "../api/research";
import { useApiResource } from "../hooks/useApiResource";
import {
  coverageRequest,
  defaultCoverageRow,
  fetchFactorCoverage,
} from "../research/factor-coverage";
import type { FactorRow, FormState } from "../research/form";

export function FactorDataHint({
  definition,
  form,
  options,
  row,
}: {
  definition: FactorDefinition;
  form: FormState;
  options: ResearchOptions;
  row?: FactorRow;
}) {
  const [open, setOpen] = useState(false);
  const [requested, setRequested] = useState<string | null>(null);
  let body = null,
    validation = null;
  try {
    body = coverageRequest(
      form,
      options,
      row ?? defaultCoverageRow(definition),
    );
  } catch (error) {
    validation =
      error instanceof Error ? error.message : "请先完善股票池与日期。";
  }
  const signature = JSON.stringify(body ?? validation);
  useEffect(() => {
    if (open) setRequested(signature);
  }, [open, signature]);
  const resource = useApiResource(
    body && requested === signature
      ? (signal) => fetchFactorCoverage(body!, signal)
      : null,
    [signature, requested],
  );
  const data = resource.data;
  const hasMissing = !resource.error && data != null && data.unavailable > 0;
  const content = (
    <Flex
      vertical
      gap={10}
      style={{ maxWidth: 370 }}
      onClick={(event) => event.stopPropagation()}
      onMouseDown={(event) => {
        event.preventDefault();
        event.stopPropagation();
      }}
    >
      <Typography.Text>{definition.description}</Typography.Text>
      {definition.formula ? (
        <Typography.Text type="secondary">
          公式：{definition.formula}
        </Typography.Text>
      ) : null}
      <Typography.Text type="secondary">
        {definition.direction === "lower"
          ? "原始值越低，得分越高。"
          : "原始值越高，得分越高。"}
      </Typography.Text>
      {validation ? (
        <Typography.Text type="secondary">{validation}</Typography.Text>
      ) : resource.error ? (
        <>
          <Alert
            type="warning"
            showIcon
            title="暂时无法核验覆盖"
            description={resource.error}
          />
          <Button size="small" onClick={resource.reload}>
            重试
          </Button>
        </>
      ) : !data ? (
        <Flex gap={8}>
          <Spin size="small" />
          <Typography.Text type="secondary">核验当前股票池</Typography.Text>
        </Flex>
      ) : (
        <>
          <Typography.Text>
            统计日期：{data.as_of}（回测首日前一交易日）
          </Typography.Text>
          <Typography.Text strong>
            可评分 {data.available} / {data.total} 只；暂不可评分{" "}
            {data.unavailable} 只
          </Typography.Text>
          {data.reasons.length ? (
            <div>
              {data.reasons.map((reason) => (
                <div key={reason.code}>
                  {reason.label}：{reason.count} 只
                </div>
              ))}
            </div>
          ) : null}
          {data.examples.length ? (
            <Typography.Text type="secondary">
              例如：
              {data.examples
                .map((item) => `${item.name}（${item.code}）`)
                .join("、")}
            </Typography.Text>
          ) : null}
          <div>
            {data.notes.map((note) => (
              <Typography.Paragraph
                key={note}
                type="secondary"
                style={{ margin: "0 0 4px" }}
              >
                {note}
              </Typography.Paragraph>
            ))}
          </div>
        </>
      )}
    </Flex>
  );
  return (
    <Popover
      title={`${definition.label} · ${hasMissing ? "数据缺失" : "数据说明"}`}
      content={content}
      trigger={["hover", "click", "focus"]}
      open={open}
      onOpenChange={(value) => {
        setOpen(value);
        if (value) setRequested(signature);
      }}
    >
      <button
        type="button"
        className={`factor-data-hint${hasMissing ? " factor-data-hint-warning" : ""}`}
        aria-label={`${definition.label}数据说明`}
        aria-expanded={open}
        onClick={(event) => event.stopPropagation()}
        onMouseDown={(event) => {
          event.preventDefault();
          event.stopPropagation();
        }}
      >
        {hasMissing ? <ExclamationCircleOutlined /> : <InfoCircleOutlined />}
      </button>
    </Popover>
  );
}
