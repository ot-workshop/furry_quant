import { useLayoutEffect, useRef, useState } from "react";
import { ApiError, newIdempotencyKey } from "../api/http";
import {
  researchApi,
  type ExperimentRequest,
  type ResearchTask,
} from "../api/research";

export const EXPERIMENT_WAIT_MS = 30_000;

export type ExperimentSubmissionState =
  | { kind: "idle" }
  | { kind: "waiting" }
  | {
      kind: "unconfirmed";
      reason: "stopped" | "timeout" | "response";
      fingerprint: string;
    }
  | { kind: "rejected"; detail: string };

type Attempt = {
  controller: AbortController;
  fingerprint: string;
  timer: ReturnType<typeof setTimeout>;
};

export function useExperimentSubmission(
  keyPrefix: string,
  onAccepted: (task: ResearchTask) => void,
  active = true,
) {
  const keys = useRef(new Map<string, string>());
  const current = useRef<Attempt | null>(null);
  const [state, setState] = useState<ExperimentSubmissionState>({
    kind: "idle",
  });

  function retire(attempt: Attempt) {
    if (current.current !== attempt) return false;
    current.current = null;
    clearTimeout(attempt.timer);
    return true;
  }

  function stop(attempt: Attempt, reason: "stopped" | "timeout") {
    if (!retire(attempt)) return;
    setState({ kind: "unconfirmed", reason, fingerprint: attempt.fingerprint });
    // Cancels fetch and response-body reads, not the server task.
    attempt.controller.abort();
  }

  useLayoutEffect(() => {
    const attempt = current.current;
    if (!active && attempt) stop(attempt, "stopped");
  }, [active]);

  useLayoutEffect(() => {
    return () => {
      const attempt = current.current;
      if (attempt && retire(attempt)) attempt.controller.abort();
    };
  }, []);

  async function submit(request: ExperimentRequest | null) {
    // A render's disabled flag alone cannot guard rapid or concurrent clicks.
    if (!active || !request || current.current) return;
    const fingerprint = JSON.stringify(request);
    let key = keys.current.get(fingerprint);
    if (!key) {
      key = newIdempotencyKey(keyPrefix);
      keys.current.set(fingerprint, key);
    }
    const attempt: Attempt = {
      controller: new AbortController(),
      fingerprint,
      timer: setTimeout(() => stop(attempt, "timeout"), EXPERIMENT_WAIT_MS),
    };
    current.current = attempt;
    setState({ kind: "waiting" });

    let task: ResearchTask;
    try {
      task = await researchApi.submitExperiment(
        request,
        key,
        attempt.controller.signal,
      );
      if (typeof task?.id !== "string" || !task.id.trim()) {
        throw new Error("Missing task ID");
      }
    } catch (error) {
      if (!retire(attempt)) return;
      setState(
        error instanceof ApiError &&
          (error.status === 422 || error.status === 409)
          ? { kind: "rejected", detail: error.message }
          : { kind: "unconfirmed", reason: "response", fingerprint },
      );
      return;
    }
    if (!retire(attempt)) return;
    setState({ kind: "idle" });
    onAccepted(task);
  }

  return {
    state,
    submit,
    stopWaiting: () => {
      if (current.current) stop(current.current, "stopped");
    },
  };
}
