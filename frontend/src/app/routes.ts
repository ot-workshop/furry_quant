import { useEffect, useSyncExternalStore } from "react";

export type Section =
  | "dashboard"
  | "research"
  | "forward-registrations"
  | "tasks"
  | "backtests"
  | "factors"
  | "evaluations"
  | "experiments"
  | "compare"
  | "data";

export interface Route {
  section: Section;
  /** Path segments after the section, decoded. */
  params: string[];
  search: URLSearchParams;
}

const SECTIONS: Record<string, Section> = {
  "": "dashboard",
  research: "research",
  "forward-registrations": "forward-registrations",
  tasks: "tasks",
  backtests: "backtests",
  factors: "factors",
  evaluations: "evaluations",
  experiments: "experiments",
  compare: "compare",
  data: "data",
};

const EVENT = "quant-route";

function subscribe(callback: () => void) {
  window.addEventListener("popstate", callback);
  window.addEventListener(EVENT, callback);
  return () => {
    window.removeEventListener("popstate", callback);
    window.removeEventListener(EVENT, callback);
  };
}

function snapshot() {
  return window.location.pathname + window.location.search;
}

export function parseRoute(path: string): Route {
  const url = new URL(path, "http://workspace.local");
  const [head = "", ...rest] = url.pathname.split("/").filter(Boolean);
  const params: string[] = [];
  for (const part of rest) {
    try {
      params.push(decodeURIComponent(part));
    } catch {
      break; // A malformed deep link falls back to the section root.
    }
  }
  return {
    section: SECTIONS[head] ?? "dashboard",
    params,
    search: url.searchParams,
  };
}

export function useRoute(): Route {
  return parseRoute(useSyncExternalStore(subscribe, snapshot));
}

export function navigate(
  path: string,
  {
    replace = false,
    returnFocusId,
    focusHeading = false,
  }: { replace?: boolean; returnFocusId?: string; focusHeading?: boolean } = {},
) {
  if (path === snapshot()) return;
  if (returnFocusId && !replace) {
    window.history.replaceState(
      {
        ...window.history.state,
        returnFocus: { id: returnFocusId, scrollY: window.scrollY },
      },
      "",
    );
  }
  window.history[replace ? "replaceState" : "pushState"](
    focusHeading ? { focusHeading: true } : null,
    "",
    path,
  );
  window.dispatchEvent(new Event(EVENT));
  if (!replace) window.scrollTo?.({ top: 0 });
}

/** Restore the exact link on a returning entry once its on-demand data is rendered. */
export function useReturnFocus(
  ready: boolean,
  path: string,
  fallback?: (id: string) => HTMLElement | null,
) {
  useEffect(() => {
    const target = window.history.state?.returnFocus;
    if (!ready || !target) return;
    const frame = requestAnimationFrame(() => {
      const link = document.getElementById(target.id) ?? fallback?.(target.id);
      if (!link) return;
      window.scrollTo({ top: target.scrollY });
      link.focus({ preventScroll: true });
      link.scrollIntoView({ block: "nearest" });
    });
    return () => cancelAnimationFrame(frame);
  }, [ready, path, fallback]);
}

/** Update query parameters of the current URL without adding history entries. */
export function setSearch(changes: Record<string, string | null | undefined>) {
  const url = new URL(window.location.href);
  for (const [key, value] of Object.entries(changes)) {
    if (value === null || value === undefined || value === "")
      url.searchParams.delete(key);
    else url.searchParams.set(key, value);
  }
  navigate(url.pathname + url.search, { replace: true });
}

const encode = encodeURIComponent;

export const paths = {
  dashboard: () => "/",
  newResearch: (fromTask?: string) =>
    `/research/new${fromTask ? `?from=${encode(fromTask)}` : ""}`,
  forwardRegistrations: () => "/forward-registrations",
  newForwardRegistration: () => "/forward-registrations/new",
  forwardRegistration: (id: string) => `/forward-registrations/${encode(id)}`,
  tasks: (focus?: string) => `/tasks${focus ? `?focus=${encode(focus)}` : ""}`,
  backtests: () => "/backtests",
  backtest: (id: string, tab?: string) =>
    `/backtests/${encode(id)}${tab ? `/${encode(tab)}` : ""}`,
  factors: (
    params: {
      run?: string;
      date?: string;
      instrument?: string;
      tab?: string;
    } = {},
  ) => {
    const search = new URLSearchParams(
      Object.entries(params).filter((entry): entry is [string, string] =>
        Boolean(entry[1]),
      ),
    ).toString();
    return `/factors${search ? `?${search}` : ""}`;
  },
  evaluations: () => "/evaluations",
  evaluation: (id: string) => `/evaluations/${encode(id)}`,
  experiments: () => "/experiments",
  newExperiment: (selection?: string) =>
    `/experiments/new${selection ? `?selection=${encode(selection)}` : ""}`,
  experiment: (id: string) => `/experiments/${encode(id)}`,
  compare: () => "/compare",
  data: (tab?: string) => `/data${tab ? `?tab=${encode(tab)}` : ""}`,
};
