import { Alert, Button, Flex } from "antd";
import { RocketOutlined } from "@ant-design/icons";
import { useLayoutEffect, useRef } from "react";
import { paths } from "../app/routes";
import type { ExperimentSubmissionState } from "../hooks/useExperimentSubmission";

const waitingEnded = {
  stopped: "已停止在此页等待。",
  timeout: "等待超时。",
  response: "没有收到可确认任务的响应。",
};

export function ExperimentSubmission({
  label,
  state,
  fingerprint,
  onSubmit,
  onStop,
}: {
  label: string;
  state: ExperimentSubmissionState;
  fingerprint: string | null;
  onSubmit: () => void;
  onStop: () => void;
}) {
  const primary = useRef<HTMLButtonElement>(null);
  const stop = useRef<HTMLButtonElement>(null);
  const status = useRef<HTMLDivElement>(null);
  const previous = useRef(state.kind);
  const waiting = state.kind === "waiting";
  const ready = fingerprint !== null;
  const changed =
    state.kind === "unconfirmed" && fingerprint !== state.fingerprint;

  useLayoutEffect(() => {
    if (waiting) stop.current?.focus();
    if (
      previous.current === "waiting" &&
      (state.kind === "unconfirmed" || state.kind === "rejected") &&
      document.activeElement === document.body
    ) {
      (state.kind === "unconfirmed" && ready
        ? primary.current
        : status.current
      )?.focus();
    }
    previous.current = state.kind;
  }, [waiting, state.kind, ready]);

  return (
    <Flex vertical gap={8} style={{ marginTop: 16 }}>
      {state.kind === "unconfirmed" ? (
        <div ref={status} tabIndex={-1}>
          <Alert
            type="warning"
            showIcon
            title="是否受理尚未确认"
            description={
              <Flex vertical gap={8} align="start">
                <span>
                  {waitingEnded[state.reason]}
                  若任务已受理，它仍会继续。可先到任务中心查看。
                  {changed
                    ? "当前参数已改动，提交可能创建另一条任务。"
                    : "按原参数重试会沿用本次提交标识。"}
                  请不要刷新此页。
                </span>
                <a
                  href={paths.tasks()}
                  target="_blank"
                  rel="noopener"
                  style={{
                    display: "inline-flex",
                    alignItems: "center",
                    minHeight: 32,
                  }}
                >
                  任务中心（新标签页）
                </a>
              </Flex>
            }
          />
        </div>
      ) : null}
      {state.kind === "rejected" ? (
        <div ref={status} tabIndex={-1}>
          <Alert
            type="error"
            showIcon
            title="提交未受理"
            description={state.detail}
          />
        </div>
      ) : null}
      <Button
        ref={primary}
        type="primary"
        block
        size="large"
        icon={<RocketOutlined />}
        disabled={!ready || waiting}
        loading={waiting}
        onClick={onSubmit}
      >
        {waiting
          ? "等待受理确认"
          : state.kind === "unconfirmed" && !changed
            ? "按原参数重试"
            : label}
      </Button>
      {waiting ? (
        <Button ref={stop} size="large" block onClick={onStop}>
          停止等待
        </Button>
      ) : null}
    </Flex>
  );
}
