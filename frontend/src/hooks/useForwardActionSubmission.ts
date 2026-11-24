import { useEffect } from "react";
import {
  forwardRegistrationsApi,
  type ForwardActionReceipt,
  type ForwardActionRequest,
  type ForwardPhase,
} from "../api/forwardRegistrations";
import { useForwardSubmission } from "./useForwardSubmission";

interface ActionRequest {
  action: ForwardActionRequest;
  previousIds: string[];
}

export function useForwardActionSubmission(
  registrationId: string,
  phase: ForwardPhase,
  refresh: () => void,
) {
  const submission = useForwardSubmission<ActionRequest, ForwardActionReceipt>({
    prefix: "forward-" + phase,
    rejectNotFound: true,
    send: async ({ action, previousIds }, key, signal) => {
      const result = await forwardRegistrationsApi.submitAction(
        registrationId,
        action,
        key,
        signal,
      );
      if (
        result.status === "accepted" &&
        (previousIds.includes(result.task_id) ||
          previousIds.includes(result.attempt_id))
      ) {
        throw new Error("Receipt refers to an earlier attempt");
      }
      return result;
    },
    onReceived: refresh,
  });

  const { state } = submission;
  useEffect(() => {
    if (state.kind === "rejected") refresh();
  }, [state, refresh]);

  return submission;
}

export type ForwardActionSubmission = ReturnType<
  typeof useForwardActionSubmission
>;
