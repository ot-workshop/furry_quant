import { useCallback, useEffect, useRef, useState } from "react";
import { describeError, isAbortError } from "../api/http";

export interface ApiState<T> {
  data: T | null;
  /** True only while there is nothing to show yet. */
  loading: boolean;
  /** True during any request, including background refreshes. */
  refreshing: boolean;
  error: string | null;
  reload: () => void;
}

interface Options {
  /** Refresh in the background every N milliseconds while mounted. */
  pollMs?: number | null;
}

/**
 * Read-only request state.
 * - Changing ``deps`` aborts the previous request and never shows the old selection's data.
 * - ``reload`` and polling keep the current data visible while refreshing (no flicker).
 * - ``load === null`` means required parameters are missing; nothing is requested.
 */
export function useApiResource<T>(
  load: ((signal: AbortSignal) => Promise<T>) | null,
  deps: readonly unknown[],
  { pollMs = null }: Options = {},
): ApiState<T> {
  const key = JSON.stringify(deps);
  const [version, setVersion] = useState(0);
  const [state, setState] = useState<{
    key: string;
    data: T | null;
    error: string | null;
  }>({
    key,
    data: null,
    error: null,
  });
  const [pending, setPending] = useState(Boolean(load));
  const loadRef = useRef(load);
  loadRef.current = load;

  const reload = useCallback(() => setVersion((value) => value + 1), []);

  useEffect(() => {
    const current = loadRef.current;
    if (!current) {
      setState({ key, data: null, error: null });
      setPending(false);
      return;
    }
    const controller = new AbortController();
    let active = true;
    setPending(true);
    setState((previous) =>
      previous.key === key ? previous : { key, data: null, error: null },
    );
    current(controller.signal)
      .then((data) => {
        if (active) setState({ key, data, error: null });
      })
      .catch((error: unknown) => {
        if (!active || isAbortError(error)) return;
        setState((previous) => ({
          key,
          data: previous.key === key ? previous.data : null,
          error: describeError(error),
        }));
      })
      .finally(() => {
        if (active) setPending(false);
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, [key, version]);

  useEffect(() => {
    if (!pollMs || !loadRef.current) return;
    const timer = window.setInterval(reload, pollMs);
    return () => window.clearInterval(timer);
  }, [pollMs, reload, key]);

  const current = state.key === key;
  const data = current ? state.data : null;
  return {
    data,
    loading:
      Boolean(load) &&
      data === null &&
      (pending || !current) &&
      !(current && state.error),
    refreshing: pending,
    error: current ? state.error : null,
    reload,
  };
}
