import { Flex, List, Tag, Typography } from "antd";
import type { MarketFailure, MarketRepair } from "../../api/data";

const CATEGORIES: Record<string, string> = {
  market_quality_requires_review: "行情质量待复核",
  secondary_suspension_activity_conflict: "停牌标记与成交冲突",
  secondary_adjustment_basis: "复权标记不一致",
  missing_pre_halt_quote: "缺少停牌前报价",
  source_conflict: "两源行情冲突",
};
const LEVELS: Record<string, string> = {
  single_source_passed: "单源检查通过",
  dual_source_verified: "双源核验通过",
  secondary_fallback_passed: "第二源补缺通过",
};
const SOURCES: Record<string, string> = {
  "tushare:daily": "官方 Tushare 日行情",
  "tushare:namechange+dated_delisting_rule":
    "历史名称变更与对应时期的退市交易规则",
  "tushare:stock_basic.list_date+exchange_calendar+dated_board_rule":
    "上市日期、交易所日历与对应板块的交易规则",
};
const DETAILS: Record<string, string> = {
  "Secondary trading activity conflicts with suspension status":
    "BaoStock 将有成交记录的日期标记为停牌。",
  "Suspension evidence has wrong instrument or adjustment basis":
    "BaoStock 返回的股票代码或复权标记与请求不一致。",
  "BaoStock adjusted returns disagree with its reported changes":
    "BaoStock 复权收益与其公布的涨跌幅不一致。",
};

export function hasMarketDetails(result?: {
  failures?: MarketFailure[];
  repairs?: MarketRepair[];
}) {
  return Boolean(result?.failures?.length || result?.repairs?.length);
}

export default function MarketJobDetails({
  failures = [],
  repairs = [],
}: {
  failures?: MarketFailure[];
  repairs?: MarketRepair[];
}) {
  return (
    <Flex vertical gap="middle">
      {failures.length ? (
        <div>
          <Typography.Text strong>
            待处理（{failures.length} 只）
          </Typography.Text>
          <List
            size="small"
            dataSource={failures}
            pagination={
              failures.length > 10 ? { pageSize: 10, size: "small" } : false
            }
            renderItem={(failure) => (
              <List.Item>
                <Flex vertical gap={4} style={{ width: "100%" }}>
                  <Typography.Text type="danger">
                    {failure.code}：
                    {CATEGORIES[failure.category ?? ""] ?? failure.reason}
                  </Typography.Text>
                  {CATEGORIES[failure.category ?? ""] ? (
                    <Typography.Text>{failure.reason}</Typography.Text>
                  ) : null}
                  {failure.dates?.length ? (
                    <Typography.Text>
                      异常日期：{failure.dates.join("、")}
                      {(failure.date_count ?? 0) > failure.dates.length
                        ? ` 等 ${failure.date_count} 个日期`
                        : ""}
                    </Typography.Text>
                  ) : null}
                  {failure.evidence?.issues?.map((issue) => (
                    <Typography.Text key={issue.rule} type="secondary">
                      {issue.message}：{issue.dates.join("、")}
                    </Typography.Text>
                  ))}
                  {failure.action ? (
                    <Typography.Text>{failure.action}</Typography.Text>
                  ) : null}
                  {failure.evidence?.quarantine_file ? (
                    <Typography.Text type="secondary" copyable>
                      隔离记录：
                      {[
                        failure.evidence.capture,
                        failure.evidence.quarantine_file,
                      ]
                        .filter(Boolean)
                        .join("/")}
                    </Typography.Text>
                  ) : null}
                </Flex>
              </List.Item>
            )}
          />
        </div>
      ) : null}
      {repairs.length ? (
        <div>
          <Typography.Text strong>
            处理记录（{repairs.length} 条）
          </Typography.Text>
          <List
            size="small"
            dataSource={repairs}
            pagination={
              repairs.length > 10 ? { pageSize: 10, size: "small" } : false
            }
            renderItem={(repair) => (
              <List.Item>
                <Flex vertical gap={4}>
                  <Typography.Text>
                    {repair.code}：{repair.reason}
                  </Typography.Text>
                  {repair.dates?.length ? (
                    <Typography.Text type="secondary">
                      日期：{repair.dates.join("、")}
                    </Typography.Text>
                  ) : null}
                  {repair.start ? (
                    <Typography.Text type="secondary">
                      补查区间：{repair.start} ~ {repair.end}
                    </Typography.Text>
                  ) : null}
                  {repair.validation_level ? (
                    <div>
                      <Tag>
                        {LEVELS[repair.validation_level] ??
                          repair.validation_level}
                      </Tag>
                    </div>
                  ) : null}
                  {repair.detail ? (
                    <Typography.Text type="secondary">
                      核验明细：{DETAILS[repair.detail] ?? repair.detail}
                    </Typography.Text>
                  ) : null}
                  {repair.source ? (
                    <Typography.Text type="secondary">
                      依据：
                      {repair.source.startsWith("https://") ? (
                        <Typography.Link
                          href={repair.source}
                          target="_blank"
                          rel="noopener noreferrer"
                        >
                          公告原文
                        </Typography.Link>
                      ) : (
                        (SOURCES[repair.source] ?? repair.source)
                      )}
                      {repair.known_by ? `；可得日期 ${repair.known_by}` : ""}
                    </Typography.Text>
                  ) : null}
                  {repair.evidence_basis ===
                  "historical_namechange_and_return_identities" ? (
                    <Typography.Text type="secondary">
                      事件日期依据历史名称变更及收益一致性检查，未经交易所事件时点认证。
                    </Typography.Text>
                  ) : null}
                </Flex>
              </List.Item>
            )}
          />
        </div>
      ) : null}
    </Flex>
  );
}
