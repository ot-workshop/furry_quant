import {
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useMediaQuery } from "../app/useMediaQuery";

export function DatasetBreakdown({
  fixed,
  historical,
  market,
  children,
}: {
  fixed: number;
  historical: number;
  market: number;
  children: ReactNode;
}) {
  const desktop = useMediaQuery("(min-width: 1024px)");
  const [visible, setVisible] = useState(1);
  const [expanded, setExpanded] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const toggle = useRef<HTMLButtonElement>(null);
  const ruler = useRef<HTMLDivElement>(null);
  const panelId = useId();
  const labels = [
    `固定股票池 ${fixed}`,
    `沪深300历史成分 ${historical}`,
    `条件筛选 ${market}`,
  ];

  useLayoutEffect(() => {
    if (!desktop || !root.current || !ruler.current) return;
    const row = root.current;
    const samples = Array.from(ruler.current.children);
    const measure = () => {
      const width = row.getBoundingClientRect().width;
      // Each sample includes the exact +N button width for that prefix.
      const fit = samples.findIndex(
        (sample) => sample.getBoundingClientRect().width <= width,
      );
      setVisible(fit < 0 ? 0 : labels.length - fit);
      if (fit === 0) {
        if (document.activeElement === toggle.current) row.focus();
        setExpanded(false);
      }
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(row);
    for (const sample of samples) observer.observe(sample);
    return () => observer.disconnect();
  }, [desktop, fixed, historical, market, labels.length]);

  useEffect(() => {
    if (!expanded || !desktop) return;
    const dismissOutside = (event: PointerEvent) => {
      if (event.target instanceof Node && !root.current?.contains(event.target))
        setExpanded(false);
    };
    document.addEventListener("pointerdown", dismissOutside);
    return () => document.removeEventListener("pointerdown", dismissOutside);
  }, [desktop, expanded]);

  if (!desktop) return <>{children}</>;

  const hidden = labels.length - visible;
  const chips = (prefix: string[]) =>
    prefix.map((label) => (
      <span key={label} className="dataset-breakdown-chip">
        {label}
      </span>
    ));
  return (
    <div
      className="dataset-breakdown"
      ref={root}
      tabIndex={-1}
      onKeyDown={(event) => {
        if (event.key === "Escape" && expanded) {
          event.stopPropagation();
          setExpanded(false);
        }
      }}
    >
      <div className="dataset-breakdown-line">
        {chips(labels.slice(0, visible))}
        {hidden > 0 ? (
          <button
            ref={toggle}
            type="button"
            className="task-reason-toggle dataset-breakdown-toggle"
            aria-label={`${expanded ? "收起" : "展开"}数据集分类，另有 ${hidden} 类`}
            aria-expanded={expanded}
            aria-controls={expanded ? panelId : undefined}
            onClick={() => setExpanded((open) => !open)}
          >
            +{hidden}
          </button>
        ) : null}
      </div>
      {expanded && hidden > 0 ? (
        <ul
          id={panelId}
          className="dataset-breakdown-panel"
          aria-label="数据集分类"
        >
          {labels.map((label) => (
            <li key={label}>{label}</li>
          ))}
        </ul>
      ) : null}
      <div className="dataset-breakdown-ruler" aria-hidden="true" ref={ruler}>
        {[3, 2, 1, 0].map((count) => (
          <div className="dataset-breakdown-line" key={count}>
            {chips(labels.slice(0, count))}
            {count < labels.length ? (
              <span className="dataset-breakdown-toggle">
                +{labels.length - count}
              </span>
            ) : null}
          </div>
        ))}
      </div>
    </div>
  );
}
