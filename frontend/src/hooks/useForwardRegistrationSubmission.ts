import {
  forwardRegistrationsApi,
  type ForwardRegistrationRequest,
} from "../api/forwardRegistrations";
import { useForwardSubmission } from "./useForwardSubmission";

function validConfirmationTime(value: unknown): boolean {
  if (
    typeof value !== "string" ||
    !/^\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d{1,6})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$/.test(
      value,
    ) ||
    !Number.isFinite(Date.parse(value))
  ) {
    return false;
  }
  // JavaScript can parse dates such as February 30 by advancing into March.
  const day = value.slice(0, 10);
  const utcDay = new Date(day);
  return (
    !day.startsWith("0000") &&
    Number.isFinite(utcDay.getTime()) &&
    utcDay.toISOString().slice(0, 10) === day
  );
}

export function useForwardRegistrationSubmission(
  onConfirmed: (id: string) => void,
) {
  return useForwardSubmission<ForwardRegistrationRequest, string>({
    prefix: "forward-registration",
    reuseKey: true,
    send: async (request, key, signal) => {
      const receipt = await forwardRegistrationsApi.create(
        request,
        key,
        signal,
      );
      if (
        typeof receipt?.registration_id !== "string" ||
        !/^fwd-[0-9a-f]{32}$/.test(receipt.registration_id) ||
        !validConfirmationTime(receipt.confirmed_at)
      ) {
        throw new Error("Invalid registration receipt");
      }
      return receipt.registration_id;
    },
    onReceived: onConfirmed,
  });
}
