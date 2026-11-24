import { Button } from "antd";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  forwardRegistrationsApi,
  type ForwardDetail,
  type ForwardPhase,
} from "../api/forwardRegistrations";
import { paths } from "../app/routes";
import { BusyButton } from "../components/BusyButton";
import { AppLink, Section } from "../components/ui";
import { useApiResource } from "../hooks/useApiResource";
import type { ForwardActionSubmission } from "../hooks/useForwardActionSubmission";

const phases = { score: "评分", verify: "到期验证" } as const;

const rejectionMessages: Record<string, string> = {
  "before-verification-maturity": "尚未到期，当前无法提交验证。",
  "registration-unsealed": "登记尚未封存。",
  "invalid-sealed-score": "评分封存证据不可用。",
  "score-snapshot-already-bound": "登记已绑定另一评分快照，请查看原版本。",
  "score-already-sealed": "评分已经封存。",
  "verification-already-sealed": "到期验证已经封存。",
  "score-capture-registration-mismatch": "该快照的登记证据与本条登记不符。",
  "score-data-end-mismatch": "该快照的评分截至日与本条登记不符。",
  "score-source-dataset-mismatch": "该快照的来源与本条登记不符。",
  "invalid-score-capture-time": "该快照的采集时间不在允许时窗内。",
};

interface ActionsProps {
  detail: ForwardDetail;
  earliest: string | null;
  readFailed: boolean;
  refreshing: boolean;
  refresh: () => void;
}

function PhaseAction({
  detail,
  phase,
  earliest,
  readFailed,
  refreshing,
  submission,
}: Omit<ActionsProps, "refresh"> & {
  phase: ForwardPhase;
  submission: ForwardActionSubmission;
}) {
  const label = phases[phase];
  const allowed = detail.available_actions?.[phase] === true;
  const [chosen, setChosen] = useState("");
  const feedback = useRef<HTMLDivElement>(null);
  const { state, submit, retry, clearRejection } = submission;
  const previousFeedbackState = useRef(state);
  const waiting = state.kind === "waiting";
  const unconfirmed = state.kind === "unconfirmed";
  const locked = waiting || unconfirmed;
  const accepted =
    state.kind === "received" && state.receipt.status === "accepted"
      ? state.receipt
      : null;
  const quoteWaiting =
    state.kind === "received" && state.receipt.status === "awaiting_quote";
  const options = useApiResource(
    phase === "score" && allowed && !detail.versions.score_snapshot
      ? (signal) => forwardRegistrationsApi.scoreOptions(detail.id, signal)
      : null,
    [detail.id, phase, allowed, detail.versions.score_snapshot],
  );
  const items = options.data?.items ?? [];
  const bound = detail.versions.score_snapshot ?? options.data?.bound;
  const selected = items.find((item) => item.id === chosen);
  const lost = chosen && options.data && !options.error && !bound && !selected;
  const snapshot = bound ?? selected?.id;
  const originalVersion =
    state.kind === "unconfirmed" &&
    state.submission.request.action.phase === "score"
      ? state.submission.request.action.scoreSnapshotId
      : null;

  const attempts = detail.attempts.filter((attempt) => attempt.kind === phase);
  const latest = attempts.sort((a, b) => b.attempt_no - a.attempt_no)[0];
  const currentAttempt =
    latest && (latest.sealed || ["queued", "running"].includes(latest.status))
      ? latest
      : null;
  const acknowledged =
    accepted &&
    attempts.some(
      (attempt) =>
        attempt.id === accepted.attempt_id &&
        attempt.task_id === accepted.task_id,
    );
  const scoreSealed = detail.attempts.some(
    (attempt) => attempt.kind === "score" && attempt.sealed,
  );
  const canSubmit =
    allowed &&
    !readFailed &&
    !refreshing &&
    !locked &&
    (!accepted || acknowledged) &&
    (phase === "verify" ||
      Boolean(snapshot && (bound || (!options.error && !options.refreshing))));

  useEffect(() => {
    if (
      state !== previousFeedbackState.current &&
      state.kind !== "idle" &&
      state.kind !== "waiting"
    ) {
      feedback.current?.focus({ preventScroll: true });
      feedback.current?.scrollIntoView({ block: "nearest" });
    }
    previousFeedbackState.current = state;
  }, [state]);

  useLayoutEffect(() => {
    const element = feedback.current;
    // A readback can insert the newly bound version above the focused receipt.
    if (element?.contains(document.activeElement)) {
      element.scrollIntoView({ block: "nearest" });
    }
  }, [detail, readFailed]);

  function submitAction() {
    if (!canSubmit) return;
    const action =
      phase === "verify"
        ? ({ phase } as const)
        : snapshot
          ? ({ phase, scoreSnapshotId: snapshot } as const)
          : null;
    if (!action) return;
    submit({
      action,
      previousIds: detail.attempts.flatMap((attempt) =>
        attempt.task_id ? [attempt.id, attempt.task_id] : [attempt.id],
      ),
    });
  }

  return (
    <section
      className="forward-phase-action"
      aria-labelledby={`forward-action-${phase}`}
    >
      <h3 id={`forward-action-${phase}`}>{label}</h3>
      {phase === "score" && bound && (
        <div className="forward-action-version">
          <span>已绑定评分快照</span>
          <code>{bound}</code>
        </div>
      )}
      {phase === "score" && allowed && !bound && (
        <div className="forward-action-selection">
          {options.loading ? (
            <p role="status">正在加载评分快照</p>
          ) : options.error ? (
            <p role="alert">评分快照读取失败。{options.error}</p>
          ) : items.length === 0 ? (
            <p role="status">暂无匹配的已发布评分快照。</p>
          ) : null}
          {lost && <p role="alert">所选评分快照已不可用，请重新选择。</p>}
          {items.length > 0 && (
            <>
              <label htmlFor="forward-action-score-options">评分快照</label>
              <select
                id="forward-action-score-options"
                value={selected?.id ?? ""}
                disabled={
                  locked || options.refreshing || Boolean(options.error)
                }
                onChange={(event) => {
                  setChosen(event.target.value);
                  clearRejection();
                }}
              >
                <option value="">请选择评分快照</option>
                {items.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.data_end} · 尾号 {item.id.slice(-12)}
                  </option>
                ))}
              </select>
              {selected && <code>{selected.id}</code>}
            </>
          )}
          <div>
            <BusyButton onClick={options.reload} busy={options.refreshing}>
              {options.error ? "重试加载评分快照" : "刷新评分快照"}
            </BusyButton>
          </div>
        </div>
      )}
      {phase === "verify" && (
        <div className="forward-action-selection">
          {earliest && <p>最早验证 {earliest}（北京时间）</p>}
          {!scoreSealed ? (
            <p>评分封存后才能提交验证。</p>
          ) : detail.state === "awaiting_exit" ? (
            <p>等待退出日。</p>
          ) : detail.versions.label_snapshot ? (
            <div className="forward-action-version">
              <span>已绑定标签快照</span>
              <code>{detail.versions.label_snapshot}</code>
            </div>
          ) : allowed && !quoteWaiting ? (
            <p>提交时由服务端查找首个合格标签。</p>
          ) : null}
        </div>
      )}
      {phase === "score" && !allowed && !bound && !currentAttempt && (
        <p>
          {detail.state === "missed"
            ? "已错过评分截止。"
            : "当前无法提交评分。"}
        </p>
      )}

      {currentAttempt && currentAttempt.id !== accepted?.attempt_id && (
        <p>
          {currentAttempt.sealed
            ? `${label}已封存。`
            : `${label}尝试已排队或正在运行。`}
          {currentAttempt.task_id && (
            <AppLink
              className="forward-link"
              to={paths.tasks(currentAttempt.task_id)}
              returnFocusId={`forward-receipt-${currentAttempt.id}`}
            >
              查看{phase === "score" ? "评分" : "验证"}任务
            </AppLink>
          )}
        </p>
      )}

      {(allowed || waiting) && !unconfirmed && (
        <div>
          <BusyButton
            type="primary"
            onClick={submitAction}
            busy={waiting}
            disabled={!canSubmit}
          >
            {`提交${label}`}
          </BusyButton>
        </div>
      )}

      {state.kind !== "idle" && (
        <div
          ref={feedback}
          id={"forward-action-feedback-" + phase}
          className="forward-action-feedback"
          tabIndex={-1}
          role={state.kind === "rejected" || unconfirmed ? "alert" : "status"}
        >
          {waiting ? (
            <p>正在提交{label}。</p>
          ) : unconfirmed ? (
            <>
              <p>尚未确认是否已入队。原参数已保留。</p>
              {originalVersion && (
                <div className="forward-action-version">
                  <span>原评分快照</span>
                  <code>{originalVersion}</code>
                </div>
              )}
              {state.detail && <p>{state.detail}</p>}
              <div>
                <Button type="primary" onClick={retry}>
                  按原参数重试
                </Button>
              </div>
            </>
          ) : state.kind === "rejected" ? (
            <>
              <p>
                {label}提交被拒绝。
                {rejectionMessages[state.detail] ?? state.detail}
              </p>
              {state.detail === "before-verification-maturity" && earliest && (
                <p>最早验证 {earliest}（北京时间）</p>
              )}
            </>
          ) : accepted ? (
            <>
              <p>
                {phase === "score" ? "评分已入队" : "验证已入队"}。尝试{" "}
                <code>{accepted.attempt_id}</code>
              </p>
              <AppLink
                className="forward-link"
                to={paths.tasks(accepted.task_id)}
                returnFocusId={`forward-receipt-${accepted.attempt_id}`}
              >
                查看{phase === "score" ? "评分" : "验证"}任务
              </AppLink>
            </>
          ) : (
            <p>
              等待绑定合格标签。本次未找到合格标签，可以稍后再提交到期验证。
            </p>
          )}
        </div>
      )}
    </section>
  );
}

export function ForwardRegistrationActions({
  submissions,
  ...props
}: ActionsProps & {
  submissions: Record<ForwardPhase, ForwardActionSubmission>;
}) {
  return (
    <Section
      title={
        <h2 id="forward-actions-heading" tabIndex={-1}>
          手动操作
        </h2>
      }
      extra={
        <BusyButton onClick={props.refresh} busy={props.refreshing}>
          刷新登记
        </BusyButton>
      }
    >
      <div className="forward-action-grid">
        <PhaseAction {...props} phase="score" submission={submissions.score} />
        <PhaseAction
          {...props}
          phase="verify"
          submission={submissions.verify}
        />
      </div>
    </Section>
  );
}
