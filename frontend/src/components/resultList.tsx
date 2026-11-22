import type { MouseEvent, ReactNode } from "react";
import { AppLink, Signed } from "./ui";
import { formatPercent } from "../utils/format";

export function ResultTitle({
  to,
  name,
  meta,
}: {
  to: string;
  name: string;
  meta: ReactNode;
}) {
  return (
    <div className="result-list-identity">
      <AppLink to={to} className="result-list-title">
        <span title={name}>{name}</span>
      </AppLink>
      <div className="result-list-meta">{meta}</div>
    </div>
  );
}

export function ResultDateRange({
  start,
  end,
}: {
  start: string;
  end: string;
}) {
  return (
    <span
      className="result-list-secondary result-list-number"
      title={`${start} ~ ${end}`}
    >
      <span className="result-list-wide-range">
        {start} ~ {end}
      </span>
      <span className="result-list-short-range">
        {start.slice(2)} ~ {end.slice(2)}
      </span>
    </span>
  );
}

export function ResultReturn({ value }: { value?: number | null }) {
  return (
    <span className="result-list-number">
      <Signed value={value} format={formatPercent} />
    </span>
  );
}

export function isResultRowBackground(event: MouseEvent) {
  return (
    !(event.target instanceof Element) || !event.target.closest("a, button")
  );
}
