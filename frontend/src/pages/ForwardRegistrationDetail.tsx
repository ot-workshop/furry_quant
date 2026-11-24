import { useCallback, useEffect, useLayoutEffect, useRef } from "react";
import type { ReactNode } from "react";
import { Alert, Tag } from "antd";
import {
  forwardRegistrationsApi,
  type ForwardAttempt,
  type ForwardDetail,
  type MissingEvidence,
} from "../api/forwardRegistrations";
import { paths, useReturnFocus } from "../app/routes";
import { useMediaQuery } from "../app/useMediaQuery";
import { BusyButton } from "../components/BusyButton";
import {
  AppLink,
  EmptyState,
  Loading,
  PageHeader,
  Section,
  StatusTag,
} from "../components/ui";
import { useApiResource } from "../hooks/useApiResource";
import { useForwardActionSubmission } from "../hooks/useForwardActionSubmission";

import { ForwardRegistrationActions } from "./ForwardRegistrationActions";

const unavailable = "暂不可用";
const shanghaiTime = new Intl.DateTimeFormat("sv-SE", {
  timeZone: "Asia/Shanghai",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
});

const states: Record<
  string,
  { label: string; reason: string; color?: string }
> = {
  unsealed: { label: "登记未封存", reason: "登记证明尚未封存。" },
  awaiting_score: { label: "待评分", reason: "等待提交评分任务。" },
  score_queued: {
    label: "评分已排队",
    reason: "等待工作进程执行评分。",
    color: "processing",
  },
  scoring: {
    label: "正在评分",
    reason: "评分任务正在运行。",
    color: "processing",
  },
  awaiting_exit: { label: "等待退出日", reason: "等待到期后验证收益。" },
  awaiting_quote: {
    label: "等待绑定合格标签",
    reason: "登记尚未绑定标签版本。",
  },
  awaiting_verification: {
    label: "待验证",
    reason: "已经绑定标签，等待提交验证任务。",
  },
  verify_queued: {
    label: "验证已排队",
    reason: "等待工作进程执行到期验证。",
    color: "processing",
  },
  verifying: {
    label: "正在验证",
    reason: "到期验证任务正在运行。",
    color: "processing",
  },
  verified: {
    label: "已封存验证结果",
    reason: "",
    color: "success",
  },
  missing_labels: {
    label: "缺少收益数据",
    reason: "已封存的验证结果包含缺失收益。",
    color: "warning",
  },
  expired: {
    label: "登记已过期",
    reason: "登记未在截止前封存。",
    color: "warning",
  },
  missed: {
    label: "已错过截止",
    reason: "已超过约定截止时间。",
    color: "warning",
  },
  invalid_evidence: {
    label: "证据无效",
    reason: "封存证据未通过一致性校验。",
    color: "error",
  },
};

const attemptReasons: Record<string, string> = {
  "deadline-passed": "已错过截止",
  "incomplete-score": "评分样本不全",
  "invalid-score-snapshot": "评分快照无效",
  "invalid-label-snapshot": "标签快照无效",
  "invalid-sealed-score": "封存评分证据无效",
  "score-execution-failed": "评分执行失败",
  "verification-execution-failed": "验证执行失败",
  "score-binding-mismatch": "评分证据与绑定快照不一致",
  "verification-binding-mismatch": "验证证据与绑定快照不一致",
  "verification-evidence-changed": "验证证据已变更",
  "attempt-failed": "尝试执行失败",
};

const evidenceReasons: Record<string, string> = {
  "missing-warmup": "动量预热数据不足",
  "missing-price": "缺少价格",
  "missing-trading-state": "缺少交易状态",
  "nonfinite-momentum": "动量值无效",
  "nonfinite-return": "收益值无效",
  "entry-missing-quote": "入场缺行情",
  "entry-missing-trading-state": "入场缺交易状态",
  "entry-suspended": "入场停牌",
  "entry-missing-price": "入场缺价格",
  "exit-missing-quote": "退出缺行情",
  "exit-missing-trading-state": "退出缺交易状态",
  "exit-suspended": "退出停牌",
  "exit-missing-price": "退出缺价格",
};

const phaseLabels = { score: "评分", verify: "到期验证" };

function moment(value: string | null) {
  const time = value ? new Date(value) : null;
  return time && Number.isFinite(time.getTime())
    ? shanghaiTime.format(time)
    : null;
}

export function factorDirection(direction: "high" | "low") {
  return direction === "high" ? "高值优先" : "低值优先";
}

export function registrationStatus(status: string) {
  return (
    (
      { registered: "已登记", scored: "已评分", failed: "失败" } as Record<
        string,
        string
      >
    )[status] ??
    states[status]?.label ??
    "状态未知"
  );
}

function Fields({ items }: { items: Array<[string, ReactNode]> }) {
  return (
    <dl className="forward-fields">
      {items.map(([label, value]) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd>{value ?? unavailable}</dd>
        </div>
      ))}
    </dl>
  );
}

function MissingRows({ items }: { items: MissingEvidence[] }) {
  return (
    <ul className="forward-missing">
      {items.map(({ code, reasons }) => (
        <li key={code}>
          <strong>{code}</strong>{" "}
          {reasons
            .map((reason) => evidenceReasons[reason] ?? "原因未知")
            .join("、")}
        </li>
      ))}
    </ul>
  );
}

function Outcome({ detail }: { detail: ForwardDetail }) {
  const { state, result, attempts } = detail;
  const currentPhase = attempts.some(
    (attempt) => attempt.kind === "score" && attempt.sealed,
  )
    ? "verify"
    : "score";
  const latest = [...attempts]
    .reverse()
    .find(
      (attempt) => attempt.kind === currentPhase && attempt.status === "failed",
    );
  const display =
    state === "failed"
      ? {
          label: `${phaseLabels[currentPhase]}失败`,
          reason: latest?.reason
            ? (attemptReasons[latest.reason] ?? "尝试执行失败")
            : "尝试执行失败",
          color: "error",
        }
      : (states[state] ?? {
          label: "状态未知",
          reason: "暂时无法确定当前阶段。",
        });
  const value = result?.excess_return_pp;
  const numeric =
    state === "verified" && typeof value === "number" && Number.isFinite(value);
  const next =
    state === "awaiting_score"
      ? moment(detail.score_deadline_at)
      : state === "awaiting_exit"
        ? moment(detail.matures_at)
        : null;
  return (
    <Section>
      <div className="forward-outcome">
        <div>
          <h2>当前阶段</h2>
          <Tag color={display.color}>{display.label}</Tag>
          {display.reason && <p>{display.reason}</p>}
          {next && (
            <p>
              {state === "awaiting_exit" ? "最早验证" : "评分截止"} {next}
              （北京时间）
            </p>
          )}
          {state === "failed" &&
            currentPhase === "score" &&
            detail.score_missing.length > 0 && (
              <MissingRows items={detail.score_missing} />
            )}
        </div>
        <div className="forward-metric">
          <h2>前 K 相对全池</h2>
          <div className="forward-metric-value">
            {numeric ? value.toFixed(2) : "—"}
            {numeric && <span> 个百分点</span>}
          </div>
          {numeric ? (
            <p>未计费复权开盘价收益差</p>
          ) : (
            <p>{state === "verified" ? "收益指标暂不可用" : display.label}</p>
          )}
          {state === "missing_labels" && result && (
            <>
              <p>
                有效收益数 · 前 K {result.candidate_valid}/{detail.top_k}，全池{" "}
                {result.pool_valid}/{detail.pool_size}
              </p>
              <MissingRows items={result.missing} />
            </>
          )}
        </div>
      </div>
    </Section>
  );
}

function AttemptLink({ attempt }: { attempt: ForwardAttempt }) {
  return attempt.task_id ? (
    <AppLink
      className="forward-link"
      to={paths.tasks(attempt.task_id)}
      returnFocusId={`forward-attempt-${attempt.id}`}
    >
      查看{phaseLabels[attempt.kind]}任务 · 第 {attempt.attempt_no} 次
    </AppLink>
  ) : (
    <span>无关联任务</span>
  );
}

function AttemptStatus({ attempt }: { attempt: ForwardAttempt }) {
  if (attempt.status === "succeeded" && !attempt.sealed)
    return <Tag color="warning">封存未确认</Tag>;
  if (attempt.status === "missed") return <Tag color="warning">已错过截止</Tag>;
  return (
    <StatusTag
      status={attempt.status}
      label={attempt.sealed ? "已封存" : undefined}
    />
  );
}

function AttemptOutcome({ attempt }: { attempt: ForwardAttempt }) {
  if (attempt.sealed)
    return `封存于 ${moment(attempt.sealed_at) ?? unavailable}`;
  if (attempt.reason) return attemptReasons[attempt.reason] ?? "尝试执行失败";
  return null;
}

function Attempts({ attempts }: { attempts: ForwardAttempt[] }) {
  const mobile = useMediaQuery("(max-width: 767px)");
  return (
    <Section title="尝试记录" description="时间均为北京时间">
      {(["score", "verify"] as const).map((kind) => {
        const timing = kind === "score" ? "评分截止" : "最早验证";
        const items = attempts
          .filter((attempt) => attempt.kind === kind)
          .sort((a, b) => b.attempt_no - a.attempt_no);
        return (
          <section key={kind} className="forward-attempt-group">
            <h3>{phaseLabels[kind]}</h3>
            {!items.length ? (
              <p>暂无{phaseLabels[kind]}尝试</p>
            ) : mobile ? (
              <ul className="forward-mobile-list">
                {items.map((attempt) => (
                  <li key={attempt.id}>
                    <strong>第 {attempt.attempt_no} 次</strong>
                    <div>
                      <AttemptStatus attempt={attempt} />
                    </div>
                    <Fields
                      items={[
                        ["创建时间", moment(attempt.created_at)],
                        ...(attempt.deadline_at
                          ? [
                              [timing, moment(attempt.deadline_at)] as [
                                string,
                                ReactNode,
                              ],
                            ]
                          : []),
                      ]}
                    />
                    <AttemptOutcome attempt={attempt} />
                    <AttemptLink attempt={attempt} />
                  </li>
                ))}
              </ul>
            ) : (
              <table
                className="forward-table forward-attempt-table"
                aria-label={`${phaseLabels[kind]}尝试`}
              >
                <thead>
                  <tr>
                    <th scope="col">尝试</th>
                    <th scope="col">状态</th>
                    <th scope="col">创建时间</th>
                    <th scope="col">{timing}</th>
                    <th scope="col">原因或封存时间</th>
                    <th scope="col">任务</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((attempt) => (
                    <tr key={attempt.id}>
                      <th scope="row">第 {attempt.attempt_no} 次</th>
                      <td>
                        <AttemptStatus attempt={attempt} />
                      </td>
                      <td>{moment(attempt.created_at) ?? unavailable}</td>
                      <td>{moment(attempt.deadline_at) ?? "—"}</td>
                      <td>
                        <AttemptOutcome attempt={attempt} />
                      </td>
                      <td>
                        <AttemptLink attempt={attempt} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
        );
      })}
    </Section>
  );
}

function actionReturnFocus(id: string) {
  if (id.startsWith("forward-receipt-")) {
    const attempt = document.getElementById(
      id.replace("forward-receipt-", "forward-attempt-"),
    );
    if (attempt) return attempt;
  }
  return document.getElementById("forward-actions-heading");
}

export function ForwardRegistrationDetail({ id }: { id: string }) {
  const state = useApiResource(
    (signal) => forwardRegistrationsApi.detail(id, signal),
    [id],
    { discardUnavailableData: true },
  );
  const detail = state.data;
  const canRefresh = useRef(Boolean(detail));
  const restoreOnSuccess = useRef(false);

  useLayoutEffect(() => {
    canRefresh.current = Boolean(detail);
  }, [detail]);

  const refreshAfterAction = useCallback(() => {
    if (canRefresh.current) state.reload();
  }, [state.reload]);
  const score = useForwardActionSubmission(id, "score", refreshAfterAction);
  const verify = useForwardActionSubmission(id, "verify", refreshAfterAction);

  useReturnFocus(Boolean(detail), id, actionReturnFocus);
  const headingRef = useRef<HTMLElement>(null);
  useEffect(() => {
    if (!detail || !window.history.state?.focusHeading) return;
    headingRef.current?.focus();
    window.history.replaceState(null, "");
  }, [detail]);
  useEffect(() => {
    if (!detail || state.error || !restoreOnSuccess.current) return;
    restoreOnSuccess.current = false;
    const statuses = { score: score.state.kind, verify: verify.state.kind };
    const phases = ["verify", "score"] as const;
    const phase =
      phases.find((item) => statuses[item] === "unconfirmed") ??
      phases.find((item) => statuses[item] === "received");
    const target = phase
      ? document.getElementById("forward-action-feedback-" + phase)
      : headingRef.current;
    target?.focus({ preventScroll: true });
    target?.scrollIntoView({ block: "nearest" });
  }, [detail, state.error, score.state.kind, verify.state.kind]);

  function retryDetail() {
    if (state.errorStatus === 403) restoreOnSuccess.current = true;
    state.reload();
  }

  return (
    <div className="forward-page">
      <PageHeader
        title={detail?.name ?? "前瞻登记详情"}
        headingRef={headingRef}
        back={{ to: paths.forwardRegistrations(), label: "返回登记列表" }}
      />
      {state.error && state.errorStatus !== 404 && (
        <Alert
          type="warning"
          title={state.errorStatus === 403 ? "详情不可用" : "详情加载失败"}
          description={state.errorStatus === 403 ? undefined : state.error}
          action={
            <BusyButton onClick={retryDetail} busy={state.refreshing}>
              重试
            </BusyButton>
          }
        />
      )}
      {state.errorStatus === 404 ? (
        <Section>
          <EmptyState title="登记不存在" />
        </Section>
      ) : !detail ? (
        state.error ? null : (
          <Loading rows={6} />
        )
      ) : (
        <>
          <Outcome detail={detail} />
          <ForwardRegistrationActions
            detail={detail}
            submissions={{ score, verify }}
            earliest={moment(detail.matures_at)}
            readFailed={Boolean(state.error)}
            refreshing={state.refreshing}
            refresh={state.reload}
          />
          <div className="forward-detail-grid">
            <Section title="冻结参数">
              <Fields
                items={[
                  [
                    "因子",
                    detail.factor === "momentum" ? "动量" : detail.factor,
                  ],
                  ["窗口", `${detail.factor_window} 个交易日`],
                  ["方向", factorDirection(detail.factor_direction)],
                  [
                    "候选与全池",
                    `前 ${detail.top_k} / 全池 ${detail.pool_size}`,
                  ],
                  [
                    "持有期",
                    detail.holding_sessions
                      ? `${detail.holding_sessions} 个交易日`
                      : unavailable,
                  ],
                  ["收益对照", "前 K 等权与登记全池等权"],
                  ["固定股票池", detail.pool_codes?.join("、") || unavailable],
                ]}
              />
            </Section>
            <Section title="日期与版本" description="时间均为北京时间">
              <Fields
                items={[
                  ["信号日", detail.signal_date],
                  ["入场日", detail.entry_date],
                  ["退出日", detail.exit_date],
                  ["评分截止", moment(detail.score_deadline_at)],
                  ["最早验证", moment(detail.matures_at)],
                  ["冻结来源", detail.versions.source_dataset],
                  ["交易日历", detail.versions.calendar],
                  ["评分快照", detail.versions.score_snapshot ?? "尚未绑定"],
                  ["标签快照", detail.versions.label_snapshot ?? "尚未绑定"],
                ]}
              />
              <details className="forward-audit">
                <summary>审计信息</summary>
                <Fields
                  items={[
                    ["登记封存时间", moment(detail.confirmed_at)],
                    ["登记指纹", detail.audit.registration_sha256],
                    ["日历指纹", detail.audit.calendar_sha256],
                    ["计算源码指纹", detail.audit.code_sha256],
                    ["依赖锁文件指纹", detail.audit.dependency_lock_sha256],
                    ...detail.attempts
                      .filter((attempt) => attempt.evidence_sha256)
                      .map((attempt): [string, ReactNode] => [
                        `${phaseLabels[attempt.kind]}第 ${attempt.attempt_no} 次证据指纹`,
                        attempt.evidence_sha256,
                      ]),
                  ]}
                />
              </details>
            </Section>
          </div>
          <Attempts attempts={detail.attempts} />
        </>
      )}
    </div>
  );
}
