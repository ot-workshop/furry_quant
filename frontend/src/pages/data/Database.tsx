import { Card, Descriptions, Flex, Statistic, Tag } from "antd";
import { api } from "../../api/client";
import { EmptyState, Resource, Section } from "../../components/ui";
import { useApiResource } from "../../hooks/useApiResource";
import { formatDateTime, formatNumber } from "../../utils/format";
export default function Database() {
  const status = useApiResource((signal) => api.dataStatus(signal), []);
  return (
    <Resource state={status}>
      {(data) =>
        data.database ? (
          <Flex vertical gap={16}>
            <div className="grid-4">
              <Card>
                <Statistic title="股票" value={data.database.securities} />
              </Card>
              <Card>
                <Statistic title="数据发布" value={data.database.releases} />
              </Card>
              <Card>
                <Statistic title="修订" value={data.database.revisions} />
              </Card>
              <Card>
                <Statistic title="冻结数据集" value={data.database.datasets} />
              </Card>
            </div>
            <Section title="行情表">
              <Descriptions
                size="small"
                column={{ xs: 1, md: 3 }}
                items={[
                  {
                    key: "range",
                    label: "覆盖区间",
                    children: data.database.coverage_start
                      ? `${data.database.coverage_start} ~ ${data.database.coverage_end}`
                      : "—",
                  },
                  {
                    key: "schema",
                    label: "结构版本",
                    children: data.database.schema_version,
                  },
                  {
                    key: "latest",
                    label: "最近发布",
                    children: data.database.latest_release
                      ? `${formatDateTime(data.database.latest_release.created_at)} · 变更 ${data.database.latest_release.changed_rows} 行`
                      : "—",
                  },
                  ...Object.entries(data.database.row_counts).map(
                    ([table, count]) => ({
                      key: table,
                      label: table,
                      children: formatNumber(count, 0),
                    }),
                  ),
                ]}
              />
            </Section>
            <Section title="数据检查">
              <Flex gap={8} wrap>
                {Object.entries(data.database.checks).map(([key, count]) => (
                  <Tag key={key}>
                    {key}：{count}
                  </Tag>
                ))}
              </Flex>
            </Section>
          </Flex>
        ) : (
          <EmptyState
            title="未连接数据库"
            description="开发环境或测试环境中没有配置 PostgreSQL 时不显示。"
          />
        )
      }
    </Resource>
  );
}
