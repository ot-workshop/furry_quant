import { useSyncExternalStore } from "react";

export type Section =
  | "dashboard"
  | "research"
  | "tasks"
  | "backtests"
  | "factors"
  | "evaluations"
  | "experiments"
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
  tasks: "tasks",
  backtests: "backtests",
  factors: "factors",
  evaluations: "evaluations",
  experiments: "experiments",
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

export function navigate(path: string, { replace = false } = {}) {
  if (path === snapshot()) return;
  window.history[replace ? "replaceState" : "pushState"](null, "", path);
  window.dispatchEvent(new Event(EVENT));
  if (!replace) window.scrollTo?.({ top: 0 });
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
  newExperiment: () => "/experiments/new",
  experiment: (id: string) => `/experiments/${encode(id)}`,
  data: (tab?: string) => `/data${tab ? `?tab=${encode(tab)}` : ""}`,
};
