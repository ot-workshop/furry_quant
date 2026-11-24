import { useLayoutEffect, useRef, useState } from "react";
import { ApiError, newIdempotencyKey } from "../api/http";

const WAIT_MS = 30_000;
const REJECTIONS = new Set([403, 409, 413, 415, 422]);

interface Submission<Request> {
  request: Request;
  key: string;
}

type State<Request, Receipt> =
  | { kind: "idle" | "waiting" }
  | { kind: "rejected"; detail: string }
  | { kind: "unconfirmed"; submission: Submission<Request>; detail?: string }
  | { kind: "received"; receipt: Receipt };

export function useForwardSubmission<Request, Receipt>({
  prefix,
  reuseKey = false,
  rejectNotFound = false,
  send,
  onReceived,
}: {
  prefix: string;
  reuseKey?: boolean;
  rejectNotFound?: boolean;
  send: (
    request: Request,
    key: string,
    signal: AbortSignal,
  ) => Promise<Receipt>;
  onReceived: (receipt: Receipt) => void;
}) {
  const [state, setState] = useState<State<Request, Receipt>>({ kind: "idle" });
  const current = useRef<AbortController | null>(null);
  const last = useRef<Submission<Request> | null>(null);

  useLayoutEffect(
    () => () => {
      current.current?.abort();
      current.current = null;
    },
    [],
  );

  async function submitOnce(submission: Submission<Request>, retrying = false) {
    if (current.current) return;
    const controller = new AbortController();
    current.current = controller;
    setState({ kind: "waiting" });
    const timer = setTimeout(() => {
      if (current.current !== controller) return;
      current.current = null;
      controller.abort();
      setState({ kind: "unconfirmed", submission });
    }, WAIT_MS);

    let receipt: Receipt;
    try {
      receipt = await send(
        submission.request,
        submission.key,
        controller.signal,
      );
    } catch (error) {
      if (current.current !== controller) return;
      current.current = null;
      if (
        error instanceof ApiError &&
        (REJECTIONS.has(error.status) ||
          (rejectNotFound && error.status === 404))
      ) {
        // These failures can occur before the server looks up the original key.
        const maintenance =
          error.status === 409 &&
          (error.code === "maintenance" ||
            error.message === "数据维护期间暂停写入，请稍后重试");
        setState(
          retrying &&
            (maintenance || [403, 404, 413, 415].includes(error.status))
            ? { kind: "unconfirmed", submission, detail: error.message }
            : { kind: "rejected", detail: error.message },
        );
      } else {
        setState({ kind: "unconfirmed", submission });
      }
      return;
    } finally {
      clearTimeout(timer);
    }

    if (current.current !== controller) return;
    current.current = null;
    setState({ kind: "received", receipt });
    onReceived(receipt);
  }

  return {
    state,
    submit(request: Request) {
      if (current.current || state.kind === "unconfirmed") return;
      const previous = last.current;
      const submission =
        reuseKey &&
        previous &&
        JSON.stringify(previous.request) === JSON.stringify(request)
          ? previous
          : { request, key: newIdempotencyKey(prefix) };
      last.current = submission;
      void submitOnce(submission);
    },
    retry() {
      if (state.kind === "unconfirmed") void submitOnce(state.submission, true);
    },
    clearRejection() {
      if (state.kind === "rejected") setState({ kind: "idle" });
    },
  };
}
