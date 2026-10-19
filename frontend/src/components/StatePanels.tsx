import type { ReactNode } from "react";

export function LoadingPanel({ label = "正在加载…" }: { label?: string }) {
  return (
    <div className="state-panel" role="status">
      <span className="spinner" aria-hidden="true" />
      <span>{label}</span>
    </div>
  );
}

interface ErrorPanelProps {
  message: string;
  onRetry?: () => void;
  title?: string;
}

export function ErrorPanel({ message, onRetry, title = "加载失败" }: ErrorPanelProps) {
  return (
    <div className="state-panel state-error" role="alert">
      <div className="state-title">{title}</div>
      <div className="state-hint">{message}</div>
      {onRetry ? (
        <button type="button" className="button button-compact" onClick={onRetry}>
          重试
        </button>
      ) : null}
    </div>
  );
}

interface EmptyPanelProps {
  title: string;
  children?: ReactNode;
}

export function EmptyPanel({ title, children }: EmptyPanelProps) {
  return (
    <div className="state-panel">
      <div className="state-title">{title}</div>
      {children ? <div className="state-hint">{children}</div> : null}
    </div>
  );
}
