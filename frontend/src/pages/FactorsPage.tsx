import { useEffect, useMemo, useState } from "react";
import {
  Button,
  Card,
  Descriptions,
  Flex,
  Segmented,
  Select,
  Statistic,
  Table,
  Tabs,
  Tag,
  Typography,
} from "antd";
import { DownloadOutlined } from "@ant-design/icons";
import { api } from "../api/client";
import { setSearch } from "../app/routes";
import { useMediaQuery } from "../app/useMediaQuery";
import { TemporalAuditPanel } from "../components/audit";
import { MISSING_POLICY_LABELS } from "../components/FactorMissingPolicy";
import { FactorCalculationDetails } from "../components/FactorCalculationDetails";
import { IndustryPanel } from "../components/backtest";
import { FactorHistoryChart } from "../components/charts";
import {
  EmptyState,
  ErrorState,
  Loading,
  MonoId,
  Notes,
  PageHeader,
  Resource,
  Section,
  TableScroll,
  WrapTag,
} from "../components/ui";
import { useApiResource } from "../hooks/useApiResource";
import type { FactorRun, FactorScore, FactorScoresResponse } from "../types";
import {
  factorLabel,
  formatNumber,
  formatPercent,
  scoreUnitLabel,
} from "../utils/format";

const PAGE = 50;

export default function FactorsPage({ search }: { search: URLSearchParams }) {
  const runs = useApiResource((signal) => api.factorRuns(signal), []);
  if (runs.data === null)
    return runs.error ? (
      <ErrorState message={runs.error} onRetry={runs.reload} />
    ) : (
      <Loading rows={8} />
    );
  if (!runs.data.items.length)
    return (
      <>
        <PageHeader title="因子评分" />
        <Card>
          <EmptyState
            title="暂无因子评分"
            description="提交研究后，每次计算的因子评分批次会出现在这里。"
          />
        </Card>
      </>
    );
  const requested = search.get("run");
  const run =
    runs.data.items.find((item) => item.id === requested) ?? runs.data.items[0];
  if (requested && run.id !== requested)
    return (
      <ErrorState
        message={`评分批次 ${requested} 不存在或暂时不可读取。`}
        onRetry={runs.reload}
      />
    );
  return <FactorWorkspace runs={runs.data.items} run={run} search={search} />;
}

function FactorWorkspace({
  runs,
  run,
  search,
}: {
  runs: FactorRun[];
  run: FactorRun;
  search: URLSearchParams;
}) {
  const isNarrowSelect = useMediaQuery("(max-width: 479px)");
  const summaryById = useMemo(
    () =>
      new Map(
        runs.map((item) => [
          item.id,
          `${item.factor_config?.length ?? 1} 个因子 · ${item.sample_size} 只`,
        ]),
      ),
    [runs],
  );
  const dates = run.available_dates?.length ? run.available_dates : [run.as_of];
  const requestedDate = search.get("date");
  const date =
    requestedDate && dates.includes(requestedDate)
      ? requestedDate
      : requestedDate
        ? (dates.filter((day) => day <= requestedDate).at(-1) ?? run.as_of)
        : run.as_of;
  const tab =
    search.get("tab") ?? (search.get("instrument") ? "stock" : "ranking");
  const instrument = search.get("instrument");

  return (
    <>
      <PageHeader
        title="因子评分"
        subtitle="同一批次内的横截面百分位评分；分数代表相对位置，不代表上涨概率"
        extra={
          <Button
            icon={<DownloadOutlined />}
            href={api.factorTableUrl(run.id, "history")}
          >
            下载完整评分（Parquet）
          </Button>
        }
      />
      <Card>
        <Flex gap={16} wrap align="flex-end">
          <Flex
            vertical
            gap={4}
            style={{ flex: "1 1 260px", minWidth: 0, maxWidth: "100%" }}
          >
            <Typography.Text type="secondary">评分批次</Typography.Text>
            <Select
              value={run.id}
              onChange={(value: string) =>
                setSearch({ run: value, date: null, instrument: null })
              }
              options={runs.map((item) => ({
                value: item.id,
                label: `${item.as_of} · ${item.sample_size} 只 · ${item.factor_config?.map((f) => factorLabel(f.name)).join("+") ?? item.scheme}`,
              }))}
              labelRender={(option) =>
                isNarrowSelect
                  ? (summaryById.get(String(option.value)) ?? option.label)
                  : option.label
              }
              showSearch
              optionFilterProp="label"
              style={{ width: "100%" }}
            />
          </Flex>
          <Flex vertical gap={4} style={{ minWidth: 0, maxWidth: "100%" }}>
            <Typography.Text type="secondary">评分日期</Typography.Text>
            <Select
              value={date}
              onChange={(value: string) => setSearch({ date: value })}
              options={[...dates]
                .reverse()
                .map((day) => ({ value: day, label: day }))}
              showSearch
              style={{ width: "min(160px, 100%)", maxWidth: "100%" }}
            />
          </Flex>
          <div className="factor-tags-wrap">
            {run.factor_config?.map((factor) => (
              <WrapTag
                key={factor.id ?? factor.name}
                text={`${factorLabel(factor.name)}${factor.window ? ` ${factor.window}日` : ""} · ${formatPercent(factor.weight, 0)}`}
              />
            ))}
          </div>
        </Flex>
      </Card>
      <Tabs
        activeKey={tab}
        onChange={(value) => setSearch({ tab: value })}
        items={[
          {
            key: "ranking",
            label: "横截面排名",
            children: <Ranking run={run} date={date} />,
          },
          {
            key: "stock",
            label: "个股详情",
            children: (
              <StockDetail run={run} date={date} instrument={instrument} />
            ),
          },
          {
            key: "industry",
            label: "行业",
            children: (
              <IndustryPanel kind="factor-runs" runId={run.id} date={date} />
            ),
          },
          {
            key: "info",
            label: "批次信息",
            children: <RunInfo run={run} date={date} />,
          },
        ]}
      />
    </>
  );
}

function Ranking({ run, date }: { run: FactorRun; date: string }) {
  const [page, setPage] = useState(1);
  const [sort, setSort] = useState<{ by: string; descending: boolean }>({
    by: "composite",
    descending: true,
  });
  useEffect(() => setPage(1), [run.id, date]);
  const scores = useApiResource(
    (signal) =>
      api.factorScores(
        run.id,
        {
          date,
          sort_by: sort.by,
          descending: sort.descending,
          offset: (page - 1) * PAGE,
          limit: PAGE,
        },
        signal,
      ),
    [run.id, date, sort.by, sort.descending, page],
  );
  const definitions = scores.data?.metadata?.factor_definitions;
  const keys = useMemo(
    () => Object.keys(scores.data?.metadata?.weights ?? {}),
    [scores.data],
  );
  return (
    <Section
      title="横截面排名"
      description={`${date} · ${scoreUnitLabel(scores.data?.metadata?.score_unit)} · 点击股票查看详情`}
    >
      <Resource
        state={scores}
        empty={(data) =>
          data.items.length ? null : <EmptyState title="该日没有评分记录" />
        }
      >
        {(data) => (
          <TableScroll label="横截面排名表">
            <Table<FactorScore>
              size="small"
              rowKey="instrument"
              dataSource={data.items}
              rowClassName={() => "clickable-row"}
              onRow={(row) => ({
                onClick: () =>
                  setSearch({ tab: "stock", instrument: row.instrument }),
              })}
              onChange={(_, __, sorter) => {
                const single = Array.isArray(sorter) ? sorter[0] : sorter;
                if (single?.columnKey && single.order) {
                  setSort({
                    by: String(single.columnKey),
                    descending: single.order === "descend",
                  });
                  setPage(1);
                }
              }}
              pagination={{
                current: page,
                pageSize: PAGE,
                total: data.total,
                onChange: setPage,
                hideOnSinglePage: true,
                showSizeChanger: false,
              }}
              columns={[
                { title: "排名", dataIndex: "rank", width: 70, align: "right" },
                {
                  title: "股票",
                  render: (_, row) => (
                    <Flex vertical>
                      <Typography.Text strong>{row.name}</Typography.Text>
                      <Typography.Text
                        type="secondary"
                        style={{ fontSize: 12 }}
                      >
                        {row.instrument}
                      </Typography.Text>
                    </Flex>
                  ),
                },
                {
                  title: "行业",
                  render: (_, row) =>
                    row.industry_status
                      ? (row.industry_name ?? "未知")
                      : (row.industry ?? "—"),
                },
                ...keys.map((key) => ({
                  title: factorLabel(key, definitions),
                  key,
                  align: "right" as const,
                  sorter: true,
                  sortOrder:
                    sort.by === key
                      ? sort.descending
                        ? ("descend" as const)
                        : ("ascend" as const)
                      : null,
                  render: (_: unknown, row: FactorScore) =>
                    formatNumber(row.scores?.[key], 1),
                })),
                {
                  title: "综合分",
                  key: "composite",
                  align: "right",
                  sorter: true,
                  sortOrder:
                    sort.by === "composite"
                      ? sort.descending
                        ? "descend"
                        : "ascend"
                      : null,
                  render: (_, row) => (
                    <Typography.Text strong>
                      {formatNumber(row.composite, 2)}
                    </Typography.Text>
                  ),
                },
              ]}
            />
          </TableScroll>
        )}
      </Resource>
    </Section>
  );
}

const REASONS: Record<string, string> = {
  insufficient_history: "历史预热不足",
  missing_or_nonpositive_pe_ttm: "PE 缺失或非正",
  missing_or_nonpositive_pb: "PB 缺失或非正",
  missing_or_nonpositive_total_mv: "市值缺失或非正",
  insufficient_negative_returns_or_history: "窗口未预热或负收益不足 2 次",
  insufficient_history_or_trading_activity:
    "预热不足、成交额异常或有效成交日不足 80%",
  suspended_auxiliary_value: "停牌沿用值不参与",
  missing_or_stale_financial_report: "财报缺失、过期或TTM历史不足",
  missing_or_stale_ttm_profit: "TTM归母净利润缺失或报告过期",
};

function StockDetail({
  run,
  date,
  instrument,
}: {
  run: FactorRun;
  date: string;
  instrument: string | null;
}) {
  const [query, setQuery] = useState("");
  const instruments = useApiResource(
    (signal) => api.instruments(run.id, "", signal),
    [run.id],
  );
  const selected = instrument ?? instruments.data?.items[0]?.instrument ?? null;
  const scores = useApiResource(
    selected
      ? (signal) =>
          api.factorScores(
            run.id,
            { date, instrument: selected, limit: 1 },
            signal,
          )
      : null,
    [run.id, date, selected],
  );
  const history = useApiResource(
    selected
      ? (signal) => api.factorHistory(selected, run.scheme, run.id, signal)
      : null,
    [run.id, run.scheme, selected],
  );
  const options = (instruments.data?.items ?? [])
    .filter((item) =>
      `${item.instrument}${item.name}`
        .toLowerCase()
        .includes(query.toLowerCase()),
    )
    .map((item) => ({
      value: item.instrument,
      label: `${item.name} ${item.instrument}`,
    }));
  const score = scores.data?.items[0] ?? null;
  return (
    <Flex vertical gap={16}>
      <Card>
        <Flex gap={24} wrap align="center">
          <Select
            showSearch
            value={selected ?? undefined}
            placeholder="搜索股票"
            filterOption={false}
            onSearch={setQuery}
            onChange={(value: string) => setSearch({ instrument: value })}
            options={options}
            style={{ width: "min(280px, 100%)", maxWidth: "100%" }}
            loading={instruments.loading}
          />
          {score ? (
            <>
              <Statistic
                title="综合分"
                value={formatNumber(score.composite, 2)}
              />
              <Statistic
                title="排名"
                value={score.rank ?? "—"}
                suffix={
                  scores.data?.metadata?.coverage
                    ? `/ ${scores.data.metadata.coverage.eligible}`
                    : undefined
                }
              />
              <Statistic
                title="行业"
                value={
                  score.industry_status
                    ? (score.industry_name ?? "未知")
                    : (score.industry ?? "—")
                }
              />
              {score.is_suspended ? <Tag color="warning">当日停牌</Tag> : null}
            </>
          ) : null}
        </Flex>
      </Card>
      {selected ? (
        <>
          <Section
            title="因子明细"
            description={`${date} · 贡献 = 得分 × 权重；缺失处理按批次配置，不重分权重`}
          >
            <Resource state={scores}>
              {(data) => (
                <FactorBreakdown
                  response={data}
                  score={score}
                  instrument={selected}
                />
              )}
            </Resource>
          </Section>
          <Section title="历史走势" description={`截至 ${date}`}>
            <Resource
              state={history}
              empty={(data) =>
                data.items.length ? null : <EmptyState title="没有历史评分" />
              }
            >
              {(data) => (
                <FactorHistoryChart
                  items={data.items.filter((row) => row.date <= date)}
                  definitions={scores.data?.metadata?.factor_definitions}
                  percentile={
                    scores.data?.metadata?.score_unit === "percentile_0_100"
                  }
                />
              )}
            </Resource>
          </Section>
        </>
      ) : (
        <EmptyState title="请选择股票" />
      )}
    </Flex>
  );
}

function FactorBreakdown({
  response,
  score,
  instrument,
}: {
  response: FactorScoresResponse;
  score: FactorScore | null;
  instrument: string;
}) {
  const metadata = response.metadata;
  const definitions = metadata?.factor_definitions ?? {};
  const exclusion = response.diagnostics?.find(
    (row) => row.instrument === instrument,
  );
  const coverage = metadata?.coverage;
  const neutral = Boolean(score?.processed);
  const rows = Object.entries(definitions).map(([key, definition]) => ({
    key,
    definition,
  }));
  return (
    <>
      {!score ? (
        <Typography.Paragraph type="warning">
          该股票当日没有综合评分
          {exclusion?.reasons._cross_section ? "：共同有效股票不足 2 只" : ""}
          {exclusion?.reasons._industry ? "：历史行业缺失或冲突" : ""}
          。下表列出各因子的原始值与排除原因。
        </Typography.Paragraph>
      ) : null}
      <TableScroll label="因子明细表">
        <Table
          size="small"
          rowKey="key"
          pagination={false}
          dataSource={rows}
          columns={[
            {
              title: "因子",
              render: (_, { key, definition }) => (
                <Flex vertical>
                  <Typography.Text>{definition.label}</Typography.Text>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    {definition.direction === "lower" ? "越低越好" : "越高越好"}{" "}
                    · 回看 {definition.lookback} 日 · {key}
                  </Typography.Text>
                </Flex>
              ),
            },
            {
              title: "原始值",
              align: "right",
              render: (_, { key }) =>
                formatNumber(score?.raw?.[key] ?? exclusion?.raw?.[key], 6),
            },
            {
              title: "得分",
              align: "right",
              render: (_, { key }) => formatNumber(score?.scores?.[key], 2),
            },
            ...(neutral
              ? [
                  {
                    title: "处理前得分",
                    align: "right" as const,
                    render: (_: unknown, { key }: { key: string }) =>
                      formatNumber(score?.before_scores?.[key], 2),
                  },
                  {
                    title: "行业残差",
                    align: "right" as const,
                    render: (_: unknown, { key }: { key: string }) =>
                      formatNumber(score?.processed?.[key], 4),
                  },
                ]
              : []),
            {
              title: "权重",
              align: "right",
              render: (_, { key }) =>
                formatPercent(metadata?.weights?.[key], 0),
            },
            {
              title: "贡献",
              align: "right",
              render: (_, { key }) =>
                formatNumber(score?.contributions?.[key], 2),
            },
            {
              title: "覆盖 / 状态",
              render: (_, { key }) => {
                const count = coverage?.factors[key];
                const reason = exclusion?.reasons[key];
                const detail =
                  score?.factor_details?.[key] ??
                  exclusion?.factor_details?.[key];
                return `${count ? `${count.valid}/${coverage?.universe} 只真实有效${count.not_applicable ? `，${count.not_applicable}只不适用` : ""} · ` : ""}${detail?.status === "not_applicable" ? (score ? "行业不适用记50分" : "行业不适用；未参与本日评分") : detail?.imputed ? MISSING_POLICY_LABELS[detail.missing_policy] : (detail?.reason_label ?? (reason ? (REASONS[reason] ?? reason) : score ? "可用" : "—"))}`;
              },
            },
          ]}
        />
      </TableScroll>
      {Object.entries(
        score?.factor_details ?? exclusion?.factor_details ?? {},
      ).map(([key, detail]) => (
        <FactorCalculationDetails
          key={key}
          label={definitions[key]?.label ?? key}
          detail={detail}
          scored={Boolean(score)}
        />
      ))}
      <Notes
        items={[
          "行情与估值按当日 18:00 可得的假设使用。",
          score?.valuation_asof && score.valuation_asof !== score.date
            ? `PE 沿用自 ${score.valuation_asof}。`
            : null,
          neutral
            ? `行业中性化：处理前综合分 ${formatNumber(score?.before_composite, 2)}，排名 ${score?.before_rank ?? "—"}。`
            : null,
        ]}
      />
    </>
  );
}

function RunInfo({ run, date }: { run: FactorRun; date: string }) {
  const scores = useApiResource(
    (signal) => api.factorScores(run.id, { date, limit: 1 }, signal),
    [run.id, date],
  );
  const [mode, setMode] = useState<"meta" | "audit">("meta");
  return (
    <Flex vertical gap={16}>
      <Segmented
        value={mode}
        onChange={(value) => setMode(value as "meta" | "audit")}
        options={[
          { value: "meta", label: "批次信息" },
          { value: "audit", label: "时点核验" },
        ]}
        style={{ width: "fit-content" }}
      />
      {mode === "meta" ? (
        <Section>
          <Descriptions
            size="small"
            column={{ xs: 1, md: 2 }}
            items={[
              {
                key: "id",
                label: "批次编号",
                children: <MonoId value={run.id} />,
              },
              {
                key: "data",
                label: "数据版本",
                children: <MonoId value={run.data_version} />,
              },
              {
                key: "dataset",
                label: "冻结数据集",
                children: <MonoId value={run.dataset_id} />,
              },
              {
                key: "scheme",
                label: "评分方案",
                children: <MonoId value={run.scheme} />,
              },
              { key: "universe", label: "股票池", children: run.universe },
              { key: "size", label: "股票数", children: run.sample_size },
            ]}
          />
        </Section>
      ) : (
        <TemporalAuditPanel audit={scores.data?.metadata?.temporal_audit} />
      )}
    </Flex>
  );
}
