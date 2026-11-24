import { query, request, requestWithStatus } from "./http";

export interface ForwardRegistration {
  id: string;
  name: string;
  status: string;
  signal_date: string;
  entry_date: string;
  exit_date: string;
  pool_size: number;
  top_k: number;
  factor: string;
  factor_window: number;
  factor_direction: "high" | "low";
  created_at: string;
}

export interface ForwardAttempt {
  id: string;
  kind: "score" | "verify";
  attempt_no: number;
  status: string;
  task_id: string | null;
  deadline_at: string | null;
  created_at: string;
  sealed: boolean;
  sealed_at: string | null;
  evidence_sha256: string | null;
  reason: string | null;
}

export interface MissingEvidence {
  code: string;
  reasons: string[];
}

export interface ForwardDetail extends ForwardRegistration {
  state: string;
  pool_codes: string[] | null;
  holding_sessions: number | null;
  confirmed: boolean;
  confirmed_at: string | null;
  score_deadline_at: string | null;
  matures_at: string | null;
  available_actions: { score: boolean; verify: boolean };
  versions: {
    calendar: string | null;
    source_dataset: string | null;
    score_snapshot: string | null;
    label_snapshot: string | null;
  };
  audit: {
    registration_sha256: string | null;
    calendar_sha256: string | null;
    code_sha256: string | null;
    dependency_lock_sha256: string | null;
  };
  attempts: ForwardAttempt[];
  score_missing: MissingEvidence[];
  result: {
    candidate_valid: number;
    pool_valid: number;
    candidate_return: number | null;
    pool_return: number | null;
    excess_return_pp: number | null;
    candidate_codes: string[];
    missing: MissingEvidence[];
  } | null;
}

interface ForwardPage {
  items: ForwardRegistration[];
  total: number;
  offset: number;
  limit: number;
}

export interface ForwardSource {
  id: string;
  name: string;
  start: string | null;
  end: string | null;
}

export interface ForwardRegistrationRequest {
  name: string;
  signal_date: string;
  pool_codes: string[];
  top_k: number;
  factor_window: number;
  factor_direction: "high" | "low";
  dataset_id: string;
}

export interface ForwardRegistrationReceipt {
  registration_id: string;
  confirmed_at: string;
}

export type ForwardPhase = "score" | "verify";

export interface ScoreOption {
  id: string;
  data_end: string;
  captured_at: string;
}

export type ForwardActionRequest =
  | { phase: "score"; scoreSnapshotId: string }
  | { phase: "verify" };

export type ForwardActionReceipt =
  | { status: "awaiting_quote" }
  | { status: "accepted"; task_id: string; attempt_id: string };

const ROOT = "/forward-registrations";

export const forwardRegistrationsApi = {
  sources: (offset: number, limit: number, signal: AbortSignal) =>
    request<{ items: ForwardSource[]; has_more: boolean }>(
      ROOT + "/sources" + query({ offset, limit }),
      { signal },
    ),
  source: (id: string, signal: AbortSignal) =>
    request<ForwardSource>(ROOT + "/sources/" + encodeURIComponent(id), {
      signal,
    }),
  create: (
    body: ForwardRegistrationRequest,
    key: string,
    signal: AbortSignal,
  ) =>
    request<ForwardRegistrationReceipt>(ROOT, {
      method: "POST",
      body,
      headers: { "Idempotency-Key": key },
      signal,
    }),
  list: (offset: number, limit: number, signal: AbortSignal) =>
    request<ForwardPage>(ROOT + query({ offset, limit }), { signal }),
  detail: (id: string, signal: AbortSignal) =>
    request<ForwardDetail>(`${ROOT}/${encodeURIComponent(id)}`, { signal }),
  scoreOptions: (id: string, signal: AbortSignal) =>
    request<{ bound: string | null; items: ScoreOption[] }>(
      ROOT + "/" + encodeURIComponent(id) + "/score-options",
      { signal },
    ),
  submitAction: async (
    id: string,
    action: ForwardActionRequest,
    key: string,
    signal: AbortSignal,
  ) => {
    const { status, data } = await requestWithStatus<ForwardActionReceipt>(
      ROOT + "/" + encodeURIComponent(id) + "/" + action.phase,
      {
        method: "POST",
        body:
          action.phase === "score"
            ? { score_snapshot_id: action.scoreSnapshotId }
            : {},
        headers: { "Idempotency-Key": key },
        signal,
      },
    );
    if (
      status === 200 &&
      action.phase === "verify" &&
      data?.status === "awaiting_quote"
    ) {
      return data;
    }
    if (
      status === 202 &&
      data?.status === "accepted" &&
      typeof data.task_id === "string" &&
      /^task-[0-9a-f]{32}$/.test(data.task_id) &&
      typeof data.attempt_id === "string" &&
      /^fat-[0-9a-f]{32}$/.test(data.attempt_id)
    ) {
      return data;
    }
    throw new Error("Invalid forward action receipt");
  },
};
