import { useLayoutEffect, useRef, useState } from "react";
import {
  forwardRegistrationsApi,
  type ForwardRegistrationRequest,
} from "../api/forwardRegistrations";
import { ApiError, newIdempotencyKey } from "../api/http";

const WAIT_MS = 30_000;
const REJECTIONS = new Set([403, 409, 413, 415, 422]);

interface Submission {
  request: ForwardRegistrationRequest;
  key: string;
}

type State =
  | { kind: "idle" | "waiting" }
  | { kind: "rejected"; detail: string }
  | { kind: "unconfirmed"; submission: Submission };

export function useForwardRegistrationSubmission(
  onConfirmed: (id: string) => void,
) {
  const [state, setState] = useState<State>({ kind: "idle" });
  const current = useRef<AbortController | null>(null);
  const last = useRef<Submission | null>(null);

  useLayoutEffect(
    () => () => {
      current.current?.abort();
      current.current = null;
    },
    [],
  );

  async function send(submission: Submission) {
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

    let id: string;
    try {
      const receipt = await forwardRegistrationsApi.create(
        submission.request,
        submission.key,
        controller.signal,
      );
      if (
        typeof receipt?.registration_id !== "string" ||
        !/^fwd-[0-9a-f]{32}$/.test(receipt.registration_id) ||
        typeof receipt.confirmed_at !== "string" ||
        !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/.test(
          receipt.confirmed_at,
        ) ||
        !Number.isFinite(Date.parse(receipt.confirmed_at))
      ) {
        throw new Error("Invalid registration receipt");
      }
      id = receipt.registration_id;
    } catch (error) {
      if (current.current !== controller) return;
      current.current = null;
      setState(
        error instanceof ApiError && REJECTIONS.has(error.status)
          ? { kind: "rejected", detail: error.message }
          : { kind: "unconfirmed", submission },
      );
      return;
    } finally {
      clearTimeout(timer);
    }

    if (current.current !== controller) return;
    current.current = null;
    onConfirmed(id);
  }

  return {
    state,
    submit(request: ForwardRegistrationRequest) {
      if (current.current || state.kind === "unconfirmed") return;
      const previous = last.current;
      const submission =
        previous && JSON.stringify(previous.request) === JSON.stringify(request)
          ? previous
          : { request, key: newIdempotencyKey("forward-registration") };
      last.current = submission;
      void send(submission);
    },
    retry() {
      if (state.kind === "unconfirmed") void send(state.submission);
    },
    clearRejection() {
      if (state.kind === "rejected") setState({ kind: "idle" });
    },
  };
}
